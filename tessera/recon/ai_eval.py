"""Measure column-mapping accuracy over the layout set: rules only, LLM only, and the hybrid actually used."""
from __future__ import annotations
import tempfile, time
from pathlib import Path
from . import ai_mapping, importer, layouts

def _score(expected: dict, got: dict) -> tuple[int, int, int]:
    right = sum(got.get(k) == v for k, v in expected.items())
    wrong = sum(1 for k, v in got.items() if k in expected and v != expected[k])    # confidently wrong, not merely missing
    return right, wrong, len(expected)

def mapping_eval(src_dir: str | Path, approaches=("rules", "llm_only", "hybrid"), held_out: bool = False) -> dict:
    L = layouts.render(Path(src_dir), Path(tempfile.mkdtemp()), layouts.HELD_OUT if held_out else None); out = {}
    for ap in approaches:
        fields = right = wrong = full = unflagged_wrong = 0; secs = 0.0; rows = []
        for x in L:
            t = time.time(); path = Path(x["path"]); hdr = importer.read_header(path)
            if ap == "rules": m = importer.propose_mapping(x["kind"], hdr); probs = ai_mapping.check_mapping(x["kind"], path, m)
            elif ap == "hybrid": r = ai_mapping.resolve(x["kind"], path); m, probs = r["mapping"], r["problems"]
            else:
                allf = list(ai_mapping.FIELD_HELP[x["kind"]]); m, _ = ai_mapping.llm_propose(x["kind"], hdr, ai_mapping.sample_rows(path), {}, allf)
                probs = ai_mapping.check_mapping(x["kind"], path, m)
            secs += time.time() - t; r_, w_, n_ = _score(x["expected"], m)
            fields += n_; right += r_; wrong += w_; full += (r_ == n_)
            unflagged_wrong += (w_ > 0 and not probs)          # wrong mapping that validation did NOT catch: the dangerous case
            rows.append({"layout": x["name"], "right": r_, "of": n_, "wrong": w_, "validation_problems": len(probs)})
        out[ap] = {"layouts_fully_correct": full, "layouts": len(L), "fields_correct": right, "fields": fields, "fields_wrong": wrong,
                   "wrong_but_passed_validation": unflagged_wrong, "avg_seconds": round(secs / len(L), 1), "per_layout": rows}
    return out
