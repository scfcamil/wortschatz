# Wortschatz

Say a word to your Apple Watch, hear the translation right away, and find it in Anki later.

```
Apple Watch shortcut ──POST──▶ API Gateway ──▶ capture Lambda ──▶ DeepL API Free
        ▲                                         │
        └──── translation spoken + shown ─────────┤ single word? → DynamoDB (pending)
                                                  │
EventBridge Scheduler (hourly) ──▶ push Lambda ───┘ ──▶ AnkiWeb ──▶ Anki on phone/Mac
```

- **Sentences** are translated but never saved. A word, or a word plus its article ("il cane", "the dog"), is saved.
- **German is always the front of the card**, with Italian or English on the back. Each word gives two cards (both directions), tagged `watch` and `it` or `en`.
- **Duplicates** are skipped twice: once at capture (same German word) and once in Anki (front already exists).

## What you need

- AWS account and CLI logged in, Terraform ≥ 1.6, Docker with buildx (Docker Desktop is fine)
- A **DeepL API Free** key from deepl.com/pro-api. It's a separate signup from the normal DeepL app, and the key ends in `:fx`.
- An **AnkiWeb** account that already holds at least one card. Add any card in Anki and sync once. Without that, the push Lambda stops rather than risk uploading a collection over yours.

## Deploy

```bash
./deploy.sh                       # region defaults to eu-central-1 (AWS_REGION=... to change)

# set the three secrets once (they never go into Terraform state)
aws ssm put-parameter --overwrite --name /wortschatz/deepl_key        --value 'xxxx:fx'
aws ssm put-parameter --overwrite --name /wortschatz/ankiweb_user     --value 'you@example.com'
aws ssm put-parameter --overwrite --name /wortschatz/ankiweb_password --value '...'

terraform -chdir=terraform output -raw api_url
terraform -chdir=terraform output -raw api_secret
```

Quick test from the terminal:

```bash
curl -s -X POST "$(terraform -chdir=terraform output -raw api_url)" \
  -H "x-api-key: $(terraform -chdir=terraform output -raw api_secret)" \
  -d '{"text":"il cane","from":"it","to":"de"}'
# {"translation": "der Hund", "de": "der Hund", "other": "il cane", "display": "der Hund – il cane ✓", "saved": true, ...}

aws lambda invoke --function-name wortschatz-push /dev/stdout   # push now instead of waiting for the hour
```

If the apply fails on `reserved_concurrent_executions`, your account has the new-account Lambda quota of 10. Either request a quota increase, or deploy with `-var push_reserved_concurrency=-1`. Runs are hourly and take seconds, so overlaps are unlikely anyway.

## Anki side

Nothing to install. Cards arrive in the deck **Wortschatz**, using your collection's own *Basic (and reversed card)* note type, so it works whatever language Anki is set to. Sync on the phone and they're there.

To use your own note type instead, set `NOTETYPE` on the push Lambda. The German word goes in the first field and the translation in the second.

## Tests

```bash
pip install anki "moto[dynamodb,s3,ssm]" pytest
pytest -q tests/
```

The end-to-end test runs Anki's open-source sync server locally in place of AnkiWeb. It sends words through the capture handler (with DeepL mocked), runs the push Lambda against moto, and checks that a second "phone" client receives the cards.
