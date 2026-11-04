#!/usr/bin/env python3

import glob
import json
import os
import platform
import shutil
import sys
import tempfile

import redis
import requests
from minio import Minio

# Hosts come from environment variables, so the same code works on
# localhost (development) and inside Kubernetes.
redisHost = os.getenv("REDIS_HOST") or "localhost"
redisPort = int(os.getenv("REDIS_PORT") or 6379)
minioHost = os.getenv("MINIO_HOST") or "localhost:9000"
minioUser = os.getenv("MINIO_USER") or "rootuser"
minioPass = os.getenv("MINIO_PASSWORD") or "rootpass123"

# FAKE_DEMUCS=1 skips the real separation and copies the input into four
# fake tracks. It is only for testing the plumbing on a small machine.
FAKE = os.getenv("FAKE_DEMUCS") == "1"

QUEUE_BUCKET = "queue"
OUTPUT_BUCKET = "output"
TRACKS = ["bass", "drums", "vocals", "other"]

redisClient = redis.StrictRedis(host=redisHost, port=redisPort, db=0)
minioClient = Minio(minioHost, access_key=minioUser,
                    secret_key=minioPass, secure=False)

infoKey = "{}.worker.info".format(platform.node())
debugKey = "{}.worker.debug".format(platform.node())


def log_debug(message, key=debugKey):
    print("DEBUG:", message, file=sys.stdout)
    sys.stdout.flush()
    redisClient.lpush('logging', f"{debugKey}:{message}")


def log_info(message, key=infoKey):
    print("INFO:", message, file=sys.stdout)
    sys.stdout.flush()
    redisClient.lpush('logging', f"{infoKey}:{message}")


def ensure_buckets():
    for bucket in (QUEUE_BUCKET, OUTPUT_BUCKET):
        if not minioClient.bucket_exists(bucket):
            minioClient.make_bucket(bucket)


def separate(songhash):
    workdir = tempfile.mkdtemp()
    try:
        inpath = os.path.join(workdir, f"{songhash}.mp3")
        outdir = os.path.join(workdir, "output")
        os.makedirs(outdir)

        minioClient.fget_object(QUEUE_BUCKET, f"{songhash}.mp3", inpath)
        log_info(f"Fetched {songhash}.mp3 from the {QUEUE_BUCKET} bucket")

        if FAKE:
            songdir = os.path.join(outdir, "fake_model", songhash)
            os.makedirs(songdir)
            for t in TRACKS:
                shutil.copy(inpath, os.path.join(songdir, f"{t}.mp3"))
            rc = 0
        else:
            rc = os.system(
                f"python3 -m demucs.separate --mp3 --out {outdir} {inpath}")

        if rc != 0:
            log_debug(f"demucs failed for {songhash} with code {rc}")
            return False

        # The model folder name depends on the demucs version, so search for it.
        files = glob.glob(os.path.join(outdir, "*", songhash, "*.mp3"))
        if not files:
            log_debug(f"no output files found for {songhash}")
            return False

        for path in files:
            track = os.path.basename(path)          # e.g. vocals.mp3
            minioClient.fput_object(OUTPUT_BUCKET, f"{songhash}-{track}", path)
            log_info(f"Uploaded {songhash}-{track}")
        return True
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def send_callback(callback):
    if not callback or not callback.get("url"):
        return
    try:
        requests.post(callback["url"], json=callback.get("data"), timeout=5)
        log_info(f"Callback sent to {callback['url']}")
    except Exception as e:
        log_debug(f"Callback failed (ignored): {e}")


ensure_buckets()
log_info(f"Worker started, FAKE_DEMUCS={FAKE}")

while True:
    try:
        # The REST server uses lpush, so brpop takes the oldest request first.
        work = redisClient.brpop("toWorker", timeout=0)
        message = json.loads(work[1].decode("utf-8"))
        songhash = message["hash"]
        log_info(f"Got request for {songhash}")
        if separate(songhash):
            log_info(f"Finished {songhash}")
            send_callback(message.get("callback"))
    except Exception as e:
        log_debug(f"Exception in worker loop: {e}")
