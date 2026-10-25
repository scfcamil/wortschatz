"""End-to-end test: Watch request -> capture Lambda -> DynamoDB -> push Lambda -> Anki sync server -> "phone".

AWS is mocked with moto, DeepL is mocked, and AnkiWeb is replaced by Anki's own open-source
sync server running locally. Run:  pytest -q tests/
"""
import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "capture"), str(ROOT / "push")]

SECRET = "test-secret"
FAKE_DEEPL = {("cane", "DE"): "Hund", ("il cane", "DE"): "der Hund", ("the dog", "DE"): "der Hund",
              ("Schmetterling", "EN-GB"): "butterfly", ("la casa", "DE"): "das Haus",
              ("dove si trova la stazione", "DE"): "Wo ist der Bahnhof?"}


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture(scope="module")
def sync_server(tmp_path_factory):
    port = free_port()
    env = dict(os.environ, SYNC_USER1="cami:pw", SYNC_BASE=str(tmp_path_factory.mktemp("srv")),
               SYNC_HOST="127.0.0.1", SYNC_PORT=str(port))
    proc = subprocess.Popen([sys.executable, "-m", "anki.syncserver"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close(); break
        except OSError:
            time.sleep(0.2)
    yield f"http://127.0.0.1:{port}/"
    proc.terminate()


@pytest.fixture()
def aws(monkeypatch, tmp_path, sync_server):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    for k, v in {"TABLE": "words", "BUCKET": "bkt", "API_SECRET_PARAM": "/w/secret",
                 "DEEPL_KEY_PARAM": "/w/deepl", "ANKIWEB_USER_PARAM": "/w/u",
                 "ANKIWEB_PASSWORD_PARAM": "/w/p", "ANKI_ENDPOINT": sync_server,
                 "WORKDIR": str(tmp_path / "lambda")}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        ddb = boto3.client("dynamodb")
        ddb.create_table(TableName="words", KeySchema=[{"AttributeName": "word", "KeyType": "HASH"}],
                         AttributeDefinitions=[{"AttributeName": "word", "AttributeType": "S"}],
                         BillingMode="PAY_PER_REQUEST")
        boto3.client("s3").create_bucket(Bucket="bkt",
                                         CreateBucketConfiguration={"LocationConstraint": "eu-central-1"})
        ssm = boto3.client("ssm")
        for n, v in {"/w/secret": SECRET, "/w/deepl": "k:fx", "/w/u": "cami", "/w/p": "pw"}.items():
            ssm.put_parameter(Name=n, Value=v, Type="SecureString")
        yield


@pytest.fixture()
def capture(aws, monkeypatch):
    import handler as h  # capture/handler.py comes first on sys.path
    import importlib; h = importlib.reload(h)

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data)
        assert req.headers["Authorization"] == "DeepL-Auth-Key k:fx"
        text = FAKE_DEEPL[(body["text"][0], body["target_lang"])]
        return io.BytesIO(json.dumps({"translations": [{"text": text}]}).encode())
    monkeypatch.setattr(h.urllib.request, "urlopen", fake_urlopen)

    def call(text, src, tgt, key=SECRET):
        r = h.handler({"headers": {"X-Api-Key": key},
                       "body": json.dumps({"text": text, "from": src, "to": tgt})}, None)
        return r["statusCode"], json.loads(r["body"])
    return call


def load_push():
    import importlib.util
    spec = importlib.util.spec_from_file_location("push_handler", ROOT / "push" / "handler.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def phone(tmp_path, endpoint, upload_first=False):
    """A second Anki client, like AnkiMobile."""
    from anki.collection import Collection
    from anki.sync_pb2 import SyncCollectionResponse as S
    (tmp_path / "phone").mkdir(exist_ok=True)
    col = Collection(str(tmp_path / "phone" / "collection.anki2"))
    if upload_first:  # a real AnkiWeb account already holds some cards
        note = col.new_note(col.models.by_name("Basic"))
        note.fields[:2] = ["Hallo", "ciao"]
        col.add_note(note, col.decks.id("Default"))
    auth = col.sync_login("cami", "pw", endpoint)
    out = col.sync_collection(auth, sync_media=False)
    if out.required in (S.FULL_UPLOAD, S.FULL_DOWNLOAD, S.FULL_SYNC):
        col.close_for_full_sync()
        col.full_upload_or_download(auth=auth, server_usn=None, upload=upload_first)
        col.reopen(after_full_sync=True)
    return col


def test_capture_rules(capture):
    assert capture("cane", "it", "de", key="wrong")[0] == 401
    status, r = capture("Cane.", "it", "de")
    assert status == 200 and r["display"] == "Hund – cane ✓" and r["saved"]
    _, r = capture("Schmetterling", "de", "en")
    assert r["display"] == "Schmetterling – butterfly ✓" and r["saved"]
    _, r = capture("dove si trova la stazione", "it", "de")
    assert r["translation"] == "Wo ist der Bahnhof?" and not r["saved"]   # sentence: translated, not saved
    _, r = capture("cane", "it", "de")
    assert r["duplicate"] and not r["saved"]
    assert capture("cane", "it", "en")[0] == 400                          # German must be on one side


def test_end_to_end(capture, sync_server, tmp_path):
    # phone already has a collection on "AnkiWeb"
    p = phone(tmp_path, sync_server, upload_first=True)
    p.close()

    capture("il cane", "it", "de")
    capture("the dog", "en", "de")        # same German word -> deduped at capture
    capture("Schmetterling", "de", "en")
    capture("la casa", "it", "de")

    push = load_push()
    assert push.handler() == {"added": 3, "skipped": 0}
    assert push.handler() == {"added": 0, "skipped": 0}   # nothing pending -> no-op

    p = phone(tmp_path, sync_server)
    ids = p.find_notes('"deck:Wortschatz"')
    got = sorted((p.get_note(i).fields[0], p.get_note(i).fields[1], tuple(sorted(p.get_note(i).tags)))
                 for i in ids)
    assert got == [("Schmetterling", "butterfly", ("en", "watch")),
                   ("das Haus", "la casa", ("it", "watch")),
                   ("der Hund", "il cane", ("it", "watch"))]
    assert len(p.find_cards('"deck:Wortschatz"')) == 6   # two directions per word
    p.close()

    # a word already in Anki (e.g. added by hand on the phone) is skipped, not duplicated
    boto3.resource("dynamodb").Table("words").put_item(
        Item={"word": "x", "de": "der Hund", "other": "cane", "lang": "it", "status": "pending", "created_at": 1})
    assert load_push().handler() == {"added": 0, "skipped": 1}
