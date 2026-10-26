"""Real-data trial: import -> tie-out -> stratified sample to label -> score with confidence intervals -> report.
Needs only DuckDB (no Postgres, no login). Real statements have no ground truth, so accuracy is estimated from a labelled random sample."""
from __future__ import annotations
import argparse, csv, datetime as dt, json, re, sys
from pathlib import Path
from .. import warehouse
from . import ai_mapping, anomalies, importer, match, trial_checks, trial_stats

DIR = Path("data/trial")
VERDICTS = {"match": ("correct", "wrong", "unsure"), "exception": ("real", "missed_match", "unsure"), "flag": ("useful", "not_useful", "unsure")}
EVENT = {"match": "wrong", "exception": "missed_match", "flag": "not_useful"}
QUESTION = {"match": "Are these two records really the same payment? (correct / wrong / unsure)",
            "exception": "Is this a genuine discrepancy, or does a matching record exist that the engine missed? (real / missed_match / unsure)",
            "flag": "Would a careful reviewer want to look at this? (useful / not_useful / unsure)"}

def _safe(s: str) -> str:
    """Names interpolated into SQL come from our own rule/queue vocabularies; refuse anything that is not a plain identifier anyway."""
    if not re.fullmatch(r"[A-Za-z0-9_]+", s): raise ValueError(f"unexpected name {s!r}")
    return s

def _strata(cur) -> list[tuple[str, str, str]]:
    """(stratum name, kind, SQL returning item_key, summary, amount, date for every member). One stratum per rule, queue or flag type."""
    out = []
    for p in warehouse.query("select distinct layer, pass from recon.match order by 1, 2"):
        if p["layer"] == "L1":
            sql = f"""select m.left_key || '|' || m.right_key as item_key, 'Order ' || l.order_id || ' (' || l.order_date || ', ' || round(l.amount_paise/100.0,2) || ') <-> payment ' || g.payment_id ||
                      ' (captured ' || g.captured_on || ', ' || round(g.gross_paise/100.0,2) || ')' as summary, round(l.amount_paise/100.0,2) as amount, cast(l.order_date as varchar) as on_date
                      from recon.match m join recon.ledger l on l.order_id = m.left_key join recon.gateway_line g on g.payment_id = m.right_key where m.layer='L1' and m.pass='{_safe(p['pass'])}'"""
        else:
            sql = f"""select m.left_key || '|' || m.right_key as item_key, 'Payout ' || b.settlement_id || ' (settled ' || b.settled_on || ', net ' || round(b.net_paise/100.0,2) || ', ' || b.n_lines || ' payments) <-> bank ' || k.posted_on ||
                      ' credit ' || round(k.credit_paise/100.0,2) || ' [' || k.description || ']' as summary, round(k.credit_paise/100.0,2) as amount, cast(k.posted_on as varchar) as on_date
                      from recon.match m join recon.batch b on b.settlement_id = m.left_key join recon.bank k on cast(k.row_id as varchar) = m.right_key where m.layer='L2' and m.pass='{_safe(p['pass'])}'"""
        out.append((f"match:{p['layer']}:{p['pass']}", "match", sql))
    for e in warehouse.query("select distinct kind from recon.exceptions order by 1"):
        out.append((f"exception:{e['kind']}", "exception", f"select item_key, detail || ' (key ' || item_key || ')' as summary, round(amount_paise/100.0,2) as amount, cast(on_date as varchar) as on_date from recon.exceptions where kind='{_safe(e['kind'])}'"))
    for r in warehouse.query("select distinct rules[1] as r from recon.flag_ranked order by 1"):
        out.append((f"flag:{r['r']}", "flag", f"select cast(row_id as varchar) as item_key, payee || ' | ' || reasons[1] as summary, round(debit_paise/100.0,2) as amount, cast(posted_on as varchar) as on_date from recon.flag_ranked where rules[1]='{_safe(r['r'])}'"))
    return out

def build_workbook(n_per_stratum: int = 30, seed: int = 1, out_dir: Path | None = None, per_stratum: dict[str, int] | None = None) -> dict:
    """per_stratum overrides the sample size for named groups, so labels can go where the risk is (e.g. the amount-window rule) instead of being spread evenly."""
    per_stratum = per_stratum or {}; out_dir = out_dir or DIR; out_dir.mkdir(parents=True, exist_ok=True); rows, meta = [], {"seed": seed, "created": dt.datetime.now().isoformat(timespec="seconds"), "n_per_stratum": n_per_stratum, "overrides": per_stratum, "strata": {}}
    for name, kind, sql in _strata(None):
        N = warehouse.query(f"select count(*) as n from ({sql})")[0]["n"]
        if not N: continue
        want = int(per_stratum.get(name, n_per_stratum))
        sample = warehouse.query(f"select * from ({sql}) order by md5(item_key || '{int(seed)}') limit {want}")      # deterministic given the seed
        meta["strata"][name] = {"kind": kind, "N": N, "n": len(sample), "question": QUESTION[kind], "verdicts": VERDICTS[kind]}
        for i, s in enumerate(sample): rows.append({"sample_id": f"{name}#{i + 1}", "stratum": name, "question": QUESTION[kind], "item": s["item_key"], "summary": s["summary"], "amount": s["amount"], "date": s["on_date"], "verdict": "", "note": ""})
    with open(out_dir / "workbook.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["sample_id"]); w.writeheader(); w.writerows(rows)
    (out_dir / "workbook.meta.json").write_text(json.dumps(meta, indent=2)); (out_dir / "workbook.csv").chmod(0o600)
    return {"rows": len(rows), "strata": {k: (v["N"], v["n"]) for k, v in meta["strata"].items()}, "path": str(out_dir / "workbook.csv")}

def score_workbook(path: Path | None = None, meta_path: Path | None = None) -> dict:
    path = path or DIR / "workbook.csv"
    meta = json.loads((meta_path or Path(path).with_suffix(".meta.json")).read_text()); rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    problems, st = [], {k: {"labelled": 0, "unsure": 0, "events": 0, "blank": 0, "invalid": 0} for k in meta["strata"]}
    for r in rows:
        s = st.get(r["stratum"]); v = (r.get("verdict") or "").strip().lower()
        if s is None: problems.append(f"unknown stratum {r['stratum']!r}"); continue
        kind = meta["strata"][r["stratum"]]["kind"]
        if not v: s["blank"] += 1
        elif v not in VERDICTS[kind]: s["invalid"] += 1; problems.append(f"{r['sample_id']}: '{v}' is not one of {VERDICTS[kind]}")
        elif v == "unsure": s["unsure"] += 1
        else: s["labelled"] += 1; s["events"] += v == EVENT[kind]
    table = []
    for name, m in meta["strata"].items():
        s = st[name]; lo, hi = trial_stats.wilson(s["events"], s["labelled"]); rate = s["events"] / s["labelled"] if s["labelled"] else None
        table.append({"stratum": name, "kind": m["kind"], "N": m["N"], "sampled": m["n"], **s, "rate": rate, "low": lo if s["labelled"] else None, "high": (trial_stats.upper_bound_zero(s["labelled"]) if s["events"] == 0 and s["labelled"] else hi) if s["labelled"] else None})
    out = {"strata": table, "problems": problems[:20], "unlabelled": sum(s["blank"] for s in st.values()), "invalid": sum(s["invalid"] for s in st.values()), "claims": [], "warnings": []}
    for kind, label in (("match", "wrong auto-match rate"), ("exception", "share of open exceptions where the engine missed a real match"), ("flag", "share of flags a reviewer would NOT find useful")):
        ts = [{"N": t["N"], "n": t["labelled"], "k": t["events"]} for t in table if t["kind"] == kind]
        if not ts: continue
        e = trial_stats.stratified_rate(ts); out[kind] = {**e, "population": sum(t["N"] for t in ts), "labelled": sum(t["n"] for t in ts), "what": label}
        if e["estimate"] is not None: out["claims"].append(f"{label}: estimated {e['estimate']:.2%} (95% interval {e['low']:.2%} to {e['high']:.2%}), from {sum(t['n'] for t in ts)} labelled of {sum(t['N'] for t in ts):,} items.")
    mt = [t for t in table if t["kind"] == "match" and t["labelled"]]
    if mt and all(t["events"] == 0 for t in mt):
        n_min = min(t["labelled"] for t in mt); need = trial_stats.samples_needed(.01)
        out["claims"].append(f"No wrong matches were found in any matching rule's sample (smallest sample {n_min}). That supports 'under 1%' only for rules with at least {need} clean samples; "
                             + ", ".join(f"{t['stratum']} (n={t['labelled']})" for t in mt if t["labelled"] < need) + " are below that.")
    for t in table:
        if t["labelled"] and t["labelled"] < 30: out["warnings"].append(f"{t['stratum']}: only {t['labelled']} labelled; the interval is wide.")
        if t["sampled"] and t["unsure"] / t["sampled"] > .1: out["warnings"].append(f"{t['stratum']}: {t['unsure']} of {t['sampled']} marked unsure; treat its estimate as unreliable.")
    if out["unlabelled"]: out["warnings"].append(f"{out['unlabelled']} row(s) have no verdict yet.")
    return out

def report(out_dir: Path | None = None) -> str:
    out_dir = out_dir or DIR
    j = lambda n: json.loads((out_dir / n).read_text()) if (out_dir / n).exists() else None
    pf = [j(f"preflight_{k}.json") for k in ("bank", "gateway", "ledger")]; tie = j("tieout.json"); sc = j("scores.json"); L = ["# Real-data trial report", f"_Generated {dt.datetime.now():%Y-%m-%d %H:%M}_", ""]
    L += ["## 1. Input checks", ""]
    for p in filter(None, pf):
        L.append(f"**{p['kind']}** `{Path(p['file']).name}` (sha256 `{p.get('sha256', '')[:16]}…`): {p['rows']:,} rows, {'no blocking problems' if p['ok'] else 'BLOCKING PROBLEMS'}")
        L += [f"- {f['severity'].upper()} {f['code']}: {f['message']}" for f in p["findings"] if f["severity"] != "info"]; L.append("")
    if tie:
        m = tie["money"]; L += ["## 2. Control totals", "", f"- Engine consistent with its own inputs: **{'yes' if tie['all_consistent'] else 'NO: investigate before trusting anything below'}**",
              f"- Orders matched to a payment: {tie['count_match_rate']:.1%} by count, **{tie['value_weighted_match_rate']:.1%} by value** (the value figure is the one that matters to money)",
              f"- Gateway fee+tax rate: {tie['money']['fee_rate']:.2%}" if m.get("fee_rate") is not None else "", "", "| Check | Expected | Actual | Diff |", "|---|---:|---:|---:|"]
        L += [f"| {c['name']} | {c['expected']/100:,.2f} | {c['actual']/100:,.2f} | {c['difference']/100:,.2f} |" for c in tie["checks"]]
        L += ["", "| Left over | Items | Value |", "|---|---:|---:|"] + [f"| {k.replace('_', ' ')} | {v['n']:,} | {v['v']/100:,.2f} |" for k, v in tie["unmatched"].items()] + [""]
    if sc:
        L += ["## 3. Accuracy estimated from your labels", ""] + [f"- {c}" for c in sc["claims"]] + [""] + ["| Sample group | Population | Labelled | Unsure | Events | Rate | 95% interval |", "|---|---:|---:|---:|---:|---:|---|"]
        for t in sc["strata"]:
            rate = "" if t["rate"] is None else f"{t['rate']:.1%}"; ci = "" if t["low"] is None else f"{t['low']:.1%} to {t['high']:.1%}"
            L.append(f"| {t['stratum']} | {t['N']:,} | {t['labelled']} | {t['unsure']} | {t['events']} | {rate} | {ci} |")
        L += [""] + [f"- WARNING: {w}" for w in sc["warnings"]]
    L += ["", "## 4. What this does and does not show", "", "- It estimates accuracy on THIS data, from a random sample you judged. It says nothing about other banks, gateways or periods.",
          "- 'Unsure' items are excluded from the rates; a high unsure share means the estimate is weaker than it looks.",
          "- Missed matches are estimated only from the exceptions you sampled; matches the engine never surfaced cannot be counted.",
          "- Flags are judged for usefulness, not proven fraud. A flag nobody can confirm is not evidence either way."]
    text = "\n".join(l for l in L if l is not None); (out_dir / "report.md").write_text(text); return text

def load(bank: str, gateway: str, ledger: str, accept: bool, use_llm: bool, mappings: dict | None = None, overrides: dict | None = None) -> dict:
    """overrides: {kind: {field: column}} applied on top of the proposed mapping (e.g. add an optional column the proposal missed), then re-validated."""
    DIR.mkdir(parents=True, exist_ok=True); warehouse.cursor().execute("create schema if not exists recon"); files = {"bank": bank, "gateway": gateway, "ledger": ledger}; res = {}
    for k, p in files.items():
        # resolve the column mapping FIRST: preflighting with header guesses alone would reject files whose headers are merely unfamiliar
        r = ai_mapping.resolve(k, p, use_llm=use_llm) if not (mappings or {}).get(k) else {"mapping": mappings[k], "problems": ai_mapping.check_mapping(k, Path(p), mappings[k]), "source": "given"}
        if (overrides or {}).get(k):
            r["mapping"] = {**r["mapping"], **overrides[k]}; r["problems"] = ai_mapping.check_mapping(k, Path(p), r["mapping"]); r["source"] += "+your override"
        pf = trial_checks.preflight(p, k, r["mapping"]); (DIR / f"preflight_{k}.json").write_text(json.dumps(pf, indent=2, default=str))
        res[k] = {"mapping": r["mapping"], "problems": r["problems"], "source": r["source"], "preflight_ok": pf["ok"], "preflight_errors": [f["code"] for f in pf["findings"] if f["severity"] == "error"]}
    blocked = {k: v for k, v in res.items() if v["problems"] or not v["preflight_ok"]}
    if blocked or not accept: return {"imported": False, "blocked": bool(blocked), "files": res}
    for k, p in files.items(): importer.load(k, p, res[k]["mapping"])
    match.run(); anomalies.run(); t = trial_checks.tieout(); (DIR / "tieout.json").write_text(json.dumps(t, indent=2, default=str))
    return {"imported": True, "files": res, "tieout_consistent": t["all_consistent"], "value_weighted_match_rate": t["value_weighted_match_rate"]}

def main(argv=None) -> int:
    ap = argparse.ArgumentParser("trial"); sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("preflight"); p.add_argument("file"); p.add_argument("--kind", required=True, choices=["bank", "gateway", "ledger"]); p.add_argument("--use-llm", action="store_true", help="propose column mapping with the local model when headers are unfamiliar")
    p = sub.add_parser("normalize"); p.add_argument("file"); p.add_argument("--out", required=True); p.add_argument("--reverse", action="store_true")
    p = sub.add_parser("load"); [p.add_argument(f"--{k}", required=True) for k in ("bank", "gateway", "ledger")]; p.add_argument("--accept-mapping", action="store_true", help="import after you have read the printed mapping"); p.add_argument("--no-llm", action="store_true"); p.add_argument("--map", action="append", default=[], metavar="KIND.FIELD=COLUMN", help='add or correct one mapping, e.g. --map "ledger.reference=Gateway Ref"')
    sub.add_parser("tieout"); p = sub.add_parser("sample"); p.add_argument("--n", type=int, default=30); p.add_argument("--seed", type=int, default=1); p.add_argument("--per-stratum", help="e.g. match:L1:B_amount_window=300,match:L1:A_ref=60")
    p = sub.add_parser("score"); p.add_argument("--workbook", default=str(DIR / "workbook.csv")); sub.add_parser("report")
    a = ap.parse_args(argv)
    if a.cmd == "preflight":
        r = trial_checks.preflight(a.file, a.kind, ai_mapping.resolve(a.kind, Path(a.file), use_llm=True)["mapping"] if a.use_llm else None); print(f"{a.file}: {r['rows']:,} rows, delimiter {r.get('delimiter')!r}, dates {r.get('date_range')}")
        for f in r["findings"]: print(f"  {f['severity'].upper():5s} {f['code']:18s} {f['message']}")
        return 0 if r["ok"] else 1
    if a.cmd == "normalize": print(json.dumps(trial_checks.normalize(a.file, a.out, a.reverse))); return 0
    if a.cmd == "load":
        ov: dict = {}
        for m_ in a.map:
            left, _, col = m_.partition("="); k_, _, f_ = left.partition(".")
            if k_ not in ("bank", "gateway", "ledger") or not f_ or not col: print(f"bad --map {m_!r}; use KIND.FIELD=COLUMN"); return 2
            ov.setdefault(k_, {})[f_] = col
        r = load(a.bank, a.gateway, a.ledger, a.accept_mapping, not a.no_llm, overrides=ov)
        for k, v in r["files"].items(): print(f"{k}: mapping from {v['source']}: {v['mapping']}" + (f"\n   PROBLEMS: {v['problems']}" if v["problems"] else "") + (f"\n   preflight errors: {v['preflight_errors']}" if v["preflight_errors"] else ""))
        print("IMPORTED; tie-out consistent:" if r["imported"] else ("BLOCKED: fix the problems above." if r["blocked"] else "Read the mappings above; rerun with --accept-mapping to import."), r.get("tieout_consistent", ""))
        return 0 if r["imported"] else 1
    if a.cmd == "tieout": t = trial_checks.tieout(); (DIR / "tieout.json").parent.mkdir(parents=True, exist_ok=True); (DIR / "tieout.json").write_text(json.dumps(t, indent=2, default=str)); print(json.dumps({k: t[k] for k in ("all_consistent", "value_weighted_match_rate", "count_match_rate", "unmatched")}, indent=2)); return 0 if t["all_consistent"] else 1
    if a.cmd == "sample": print(json.dumps(build_workbook(a.n, a.seed, per_stratum={k: int(v) for k, v in (x.split("=") for x in a.per_stratum.split(","))} if a.per_stratum else None), indent=2)); return 0
    if a.cmd == "score": s = score_workbook(Path(a.workbook)); (DIR / "scores.json").write_text(json.dumps(s, indent=2)); [print("-", c) for c in s["claims"]]; [print("WARNING:", w) for w in s["warnings"]]; [print("PROBLEM:", p_) for p_ in s["problems"]]; return 1 if s["problems"] else 0
    if a.cmd == "report": print(report()); return 0
    return 1

if __name__ == "__main__":
    sys.exit(main())
