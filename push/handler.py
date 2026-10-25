"""Push Lambda: moves "pending" words from DynamoDB into Anki and syncs them to AnkiWeb.

Each run:
  1. exits early if nothing is pending (no AnkiWeb traffic)
  2. restores the last collection copy from S3 (or starts empty)
  3. syncs with AnkiWeb first (full *download* allowed - AnkiWeb is the source of truth)
  4. adds the pending words as 'Basic (and reversed card)' notes (skipping words already there)
  5. syncs again (normal sync only - never a full upload that could overwrite your phone)
  6. saves the collection back to S3 and marks the words as synced
Run with reserved concurrency 1 so two runs never touch the collection at once.
"""
import logging
import os
import pathlib
import time

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from anki.collection import Collection
from anki.notes import NoteFieldsCheckResult
from anki.stdmodels import StockNotetypeKind
from anki.sync_pb2 import SyncCollectionResponse

log = logging.getLogger()
log.setLevel(logging.INFO)

Changes = SyncCollectionResponse.ChangesRequired

DECK = os.environ.get("DECK", "Wortschatz")
NOTETYPE = os.environ.get("NOTETYPE")  # optional: name of your own note type (fields: German, other)
S3_KEY = os.environ.get("S3_KEY", "collection.anki2")
ENDPOINT = os.environ.get("ANKI_ENDPOINT") or None  # None = AnkiWeb
WORKDIR = pathlib.Path(os.environ.get("WORKDIR", "/tmp/anki"))
COL_PATH = WORKDIR / "collection.anki2"


class FullSyncRequired(RuntimeError):
    pass


def param(name):
    return boto3.client("ssm").get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def pending_items(table):
    items, kwargs = [], {"FilterExpression": Attr("status").eq("pending")}
    while True:
        page = table.scan(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            return sorted(items, key=lambda i: int(i.get("created_at", 0)))
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def restore_collection(s3, bucket):
    WORKDIR.mkdir(parents=True, exist_ok=True)
    for f in WORKDIR.glob("collection.anki2*"):  # stale copy from a warm container
        f.unlink()
    try:
        s3.download_file(bucket, S3_KEY, str(COL_PATH))
        log.info("restored collection from s3")
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchKey"):
            raise
        log.info("no collection in s3 yet - starting empty, will download from AnkiWeb")
    return Collection(str(COL_PATH))


def sync(col, auth, allow_full_download):
    out = col.sync_collection(auth, sync_media=False)
    if out.new_endpoint:
        auth.endpoint = out.new_endpoint
    if out.server_message:
        log.info("AnkiWeb says: %s", out.server_message)
    req = out.required
    if req in (Changes.NO_CHANGES, Changes.NORMAL_SYNC):
        return
    if allow_full_download and req in (Changes.FULL_DOWNLOAD, Changes.FULL_SYNC):
        log.info("full download from AnkiWeb")
        col.close_for_full_sync()
        col.full_upload_or_download(auth=auth, server_usn=None, upload=False)
        col.reopen(after_full_sync=True)
        return
    if req == Changes.FULL_UPLOAD:
        raise FullSyncRequired(
            "AnkiWeb has no collection yet - add one card in Anki on your Mac or phone and sync, then retry")
    raise FullSyncRequired(f"AnkiWeb requested a full sync ({Changes.Name(req)}) - "
                           "nothing was pushed; the words stay pending for the next run")


def find_notetype(col):
    """Use an existing note type: creating one from here is a schema change and would force a full sync."""
    if NOTETYPE:
        nt = col.models.by_name(NOTETYPE)
        if not nt:
            raise RuntimeError(f"note type {NOTETYPE!r} not found in your collection")
        return nt
    # Anki's stock "Basic (and reversed card)", whatever language the collection was created in
    # (originalStockKind is stored as enum value + 1)
    for nt in col.models.all():
        if nt.get("originalStockKind") == StockNotetypeKind.KIND_BASIC_AND_REVERSED + 1:
            return nt
    raise RuntimeError("no 'Basic (and reversed card)' note type found - set NOTETYPE to one you have")


def handler(event=None, context=None):
    table = boto3.resource("dynamodb").Table(os.environ["TABLE"])
    items = pending_items(table)
    if not items:
        return {"added": 0, "skipped": 0}

    s3 = boto3.client("s3")
    bucket = os.environ["BUCKET"]
    col = restore_collection(s3, bucket)
    try:
        auth = col.sync_login(param(os.environ["ANKIWEB_USER_PARAM"]),
                              param(os.environ["ANKIWEB_PASSWORD_PARAM"]), ENDPOINT)
        sync(col, auth, allow_full_download=True)

        nt = find_notetype(col)
        deck_id = col.decks.id(DECK)
        added = skipped = 0
        for item in items:
            note = col.new_note(nt)
            note.fields[0] = item["de"]      # front: German
            note.fields[1] = item["other"]   # back: Italian / English
            note.tags = ["watch", item.get("lang", "")]
            if note.fields_check() == NoteFieldsCheckResult.DUPLICATE:
                skipped += 1
                continue
            col.add_note(note, deck_id)
            added += 1

        if added:
            sync(col, auth, allow_full_download=False)
    except Exception:
        col.close(downgrade=False)
        raise  # S3 copy and DynamoDB stay untouched -> retried next run

    col.close(downgrade=False)
    s3.upload_file(str(COL_PATH), bucket, S3_KEY)

    now = int(time.time())
    for item in items:
        table.update_item(
            Key={"word": item["word"]},
            UpdateExpression="SET #s = :s, synced_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "synced", ":t": now},
        )
    log.info("added %d, skipped %d duplicates", added, skipped)
    return {"added": added, "skipped": skipped}
