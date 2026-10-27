import os
import tempfile

import pytest

# Force the offline engine and a throwaway database before the app is imported.
os.environ["MODEL_API_KEY"] = ""
os.environ["OPERATOR_ID"] = ""
os.environ["OPERATOR_KEY"] = ""
os.environ["SHOPIFY_STORES"] = ""  # tests that need a store use a fake UCP transport
os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "app.db")


@pytest.fixture
def settings(tmp_path):
    from globalchain.config import Settings

    return Settings(model_api_key=None, operator_id=None, operator_key=None, shopify_stores="",
                    database_path=str(tmp_path / "test.db"))


@pytest.fixture
def service(settings):
    from globalchain.engine import MockEngine
    from globalchain.provenance import NullAnchorer
    from globalchain.service import GlobalChain
    from globalchain.store import Store

    return GlobalChain(settings, MockEngine(), Store(settings.database_path), NullAnchorer())


@pytest.fixture
def demo_items():
    from globalchain.demo import _inline_static_images, load_demo_listings

    return [(_inline_static_images(listing), seller) for listing, seller in load_demo_listings()]
