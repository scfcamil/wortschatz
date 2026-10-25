"""Capture Lambda: called by the Apple Watch shortcut.

POST {"text": "cane", "from": "it", "to": "de"}   header: x-api-key: <secret>
 -> translates with DeepL API Free and answers immediately
 -> if it's a single word (or word + article) stores it in DynamoDB as "pending" for the push Lambda
"""
import base64
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.request

import boto3
from botocore.exceptions import ClientError

DEEPL_URL = os.environ.get("DEEPL_URL", "https://api-free.deepl.com/v2/translate")
MAX_WORDS = int(os.environ.get("MAX_WORDS", "2"))
# language code -> (DeepL source code, DeepL target code)
LANGS = {"de": ("DE", "DE"), "it": ("IT", "IT"), "en": ("EN", os.environ.get("EN_VARIANT", "EN-GB"))}

_params = {}
_table = None


def param(name):
    if name not in _params:
        ssm = boto3.client("ssm")
        _params[name] = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    return _params[name]


def table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE"])
    return _table


def clean(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text.strip(" .,;:!?¡¿\"'«»“”")


def word_count(text):
    return len(re.findall(r"[\w'-]+", text))


def lower_first(text):
    return text[:1].lower() + text[1:] if text else text


def translate(text, src, tgt):
    body = json.dumps({
        "text": [text],
        "source_lang": LANGS[src][0],
        "target_lang": LANGS[tgt][1],
    }).encode()
    req = urllib.request.Request(DEEPL_URL, body, {
        "Authorization": f"DeepL-Auth-Key {param(os.environ['DEEPL_KEY_PARAM'])}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)["translations"][0]["text"]


def respond(status, payload):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(payload, ensure_ascii=False),
    }


def handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not hmac.compare_digest(headers.get("x-api-key", ""), param(os.environ["API_SECRET_PARAM"])):
        return respond(401, {"error": "unauthorized"})

    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return respond(400, {"error": "body must be JSON"})

    text = clean(data.get("text", ""))
    src, tgt = str(data.get("from", "")).lower(), str(data.get("to", "")).lower()
    if not text or len(text) > 500:
        return respond(400, {"error": "text missing or too long"})
    if src not in LANGS or tgt not in LANGS or src == tgt or "de" not in (src, tgt):
        return respond(400, {"error": "from/to must be de<->it or de<->en"})

    is_word = word_count(text) <= MAX_WORDS
    if is_word and src != "de":
        text = lower_first(text)  # dictation capitalises: "Cane" -> "cane" (else DeepL may treat it as a name)

    try:
        translation = translate(text, src, tgt)
    except urllib.error.HTTPError as e:
        if e.code == 456:
            return respond(503, {"error": "DeepL monthly quota reached", "display": "DeepL quota reached"})
        return respond(502, {"error": f"DeepL error {e.code}", "display": "Translation failed"})
    except (urllib.error.URLError, TimeoutError):
        return respond(504, {"error": "DeepL unreachable", "display": "Translation failed"})

    if is_word:
        translation = clean(translation)
    if src == "de":
        de, other, lang = text, translation, tgt
    else:
        de, other, lang = translation, text, src
    is_word = is_word and word_count(de) <= MAX_WORDS + 1

    saved = duplicate = False
    if is_word:
        try:
            table().put_item(
                Item={
                    "word": de.casefold(),
                    "de": de,
                    "other": other,
                    "lang": lang,
                    "status": "pending",
                    "created_at": int(time.time()),
                },
                ConditionExpression="attribute_not_exists(word)",
            )
            saved = True
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            duplicate = True

    return respond(200, {
        "translation": translation,
        "de": de,
        "other": other,
        "display": (f"{de} – {other}" + (" ✓" if saved else " ↺" if duplicate else "")) if is_word else translation,
        "saved": saved,
        "duplicate": duplicate,
    })
