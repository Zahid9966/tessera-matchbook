import os, pytest
from tessera import config, warehouse, generate
from tessera.pipeline import jobs
from tessera.recon import pg
from tessera.recon.settings import settings

@pytest.fixture(scope="session", autouse=True)
def wh(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    config.DATA = home; config.RAW = home / "raw"; config.WAREHOUSE = home / "w.duckdb"
    generate.generate(customers=300, days=200, out=config.RAW)
    warehouse.close()
    assert jobs.run("pipeline")["status"] == "success"
    yield
    warehouse.close()


@pytest.fixture(scope="session", autouse=True)
def pgdb():
    """Postgres for the whole run: CI supplies MATCHBOOK_TEST_DATABASE_URL; otherwise a private throwaway cluster is started."""
    url = os.environ.get("MATCHBOOK_TEST_DATABASE_URL"); data = None
    if not url: url, data = pg.start_dev_cluster(dbname="matchbook_test")
    settings.database_url = url; settings.scrypt_n_exp = 10        # fast hashing in tests only; production default is 15
    pg.close(); pg.reset_for_tests()
    yield url
    pg.close()
    if data: pg.stop_dev_cluster(data, remove=True)
