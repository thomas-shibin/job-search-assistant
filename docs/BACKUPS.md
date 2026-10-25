# Backups

Every night at `BACKUP_HOUR` (3 a.m. server time by default) the site backs itself up
(`app/modules/backups/service.py`):

1. **A consistent copy of the database.** SQLite's online backup makes it while the site
   keeps running, and it must pass `PRAGMA integrity_check` before it is used.
2. **Packed with members' files:** uploaded resumes (`data/resumes`), tailored resumes and
   application files (`data/applications`) and `config/`, all in one `.tar.gz`. `config/`
   holds `free_plan.key`, which the free plan needs to recognise people from before the
   restore: restore it together with the database.
3. **Encrypted** with AES-256-GCM when `BACKUP_ENCRYPTION_KEY` is set. Only an encrypted
   backup is ever copied off the server; personal data never leaves it unencrypted.
4. **Copied to a bucket** (Cloudflare R2, Backblaze B2, AWS S3; any S3-compatible storage)
   when `BACKUP_S3_*` is set.
5. **Old backups removed**, on the server and in the bucket. It keeps the newest backup of
   each of the last 7 days, 4 weeks and 6 months (`BACKUP_KEEP_DAILY`, `_WEEKLY`, `_MONTHLY`).
   Only `hirlumo-backup-*` files are ever removed. Copies made by hand in `data/backups`
   (such as `job_assistant-pre-0.8-….db`) are left alone.

If the server was off at 3 a.m., a backup runs 10 minutes after it starts.
**Admin → Health** shows when the last backup ran and whether it reached the bucket, and has
a **Back up now** button.

`.env` is **not** in the backup. Keep its secrets in a password manager, above all
`BACKUP_ENCRYPTION_KEY`: without it the off-server copies can't be opened.

## Set it up (once)

1. **Make a key:** `sudo docker compose --env-file .env.prod exec app python cli.py backup-key`
   (or `python cli.py backup-key` anywhere). Put it in `.env.prod` as
   `BACKUP_ENCRYPTION_KEY=...` **and** in your password manager.
2. **Make a bucket.** On Cloudflare: **R2 → Create bucket**, named for example
   `hirlumo-backups`, with the default location. Then go to **R2 → Manage API tokens → Create
   API token**, with **Object Read & Write**, applied to that bucket only. Copy the access key
   ID, the secret access key and the endpoint (`https://<account id>.r2.cloudflarestorage.com`).
3. **Fill in `.env.prod`:**
   ```
   BACKUP_S3_ENDPOINT=https://<account id>.r2.cloudflarestorage.com
   BACKUP_S3_BUCKET=hirlumo-backups
   BACKUP_S3_ACCESS_KEY_ID=...
   BACKUP_S3_SECRET_ACCESS_KEY=...
   BACKUP_S3_REGION=auto
   ```
4. **Restart:** `sudo docker compose --env-file .env.prod up -d`. Then press **Back up now** on
   Admin → Health and check that it reports "encrypted and copied off the server".

R2's free tier includes 10 GB of storage, which is far more than these backups need today.

## Restore

The restore command only unpacks into a **new folder**; it never touches the running site.

```bash
# What's there (on the server and in the bucket):
sudo docker compose --env-file .env.prod exec app python cli.py backup-list

# Unpack one (downloaded from the bucket if it isn't on this server):
sudo docker compose --env-file .env.prod exec app \
  python cli.py backup-restore hirlumo-backup-20261022-030000.tar.gz.enc --to /app/data/restore-20261022
```

The folder then holds `job_assistant.db`, `resumes/`, `applications/` and `config/`.
To swap them in (the data folder is `HIRLUMO_DATA_DIR` in `.env.prod`, `/srv/hirlumo/data`
after `scripts/move-data.sh`; `./data` in the app folder before it):

```bash
sudo docker compose --env-file .env.prod down
cd /srv/hirlumo
sudo mv data data-before-restore                       # keep the current files; don't delete them
sudo mv data-before-restore/restore-20261022 data
sudo cp -a data-before-restore/backups data/            # keep the backups
sudo chown -R hirlumo:hirlumo data && sudo chmod -R u+rwX,go-rwx data
cd -  # back to the app folder
sudo docker compose --env-file .env.prod up -d
```

Then check the site.

**Practise this once** on another computer before you need it. A backup you've never
restored isn't proven to work.
