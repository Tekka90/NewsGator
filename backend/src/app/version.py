"""Server version: the release tag, injected at image build time (APP_VERSION)."""

import os

VERSION = os.environ.get("APP_VERSION", "").strip().removeprefix("v").removeprefix("V") or "dev"
