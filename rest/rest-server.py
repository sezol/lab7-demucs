#!/usr/bin/env python3

import hashlib
import io
import os
import platform
import sys
import json
import base64

import redis
from flask import Flask, request, jsonify, Response
from minio import Minio

app = Flask(__name__)

# Hosts come from environment variables, so the same code works on
# localhost (development) and inside Kubernetes.
redisHost = os.getenv("REDIS_HOST") or "localhost"
redisPort = int(os.getenv("REDIS_PORT") or 6379)
minioHost = os.getenv("MINIO_HOST") or "localhost:9000"
minioUser = os.getenv("MINIO_USER") or "rootuser"
minioPass = os.getenv("MINIO_PASSWORD") or "rootpass123"

QUEUE_BUCKET = "queue"
OUTPUT_BUCKET = "output"

redisClient = redis.StrictRedis(host=redisHost, port=redisPort, db=0)
minioClient = Minio(minioHost, access_key=minioUser,
                    secret_key=minioPass, secure=False)

infoKey = "{}.rest.info".format(platform.node())
debugKey = "{}.rest.debug".format(platform.node())


def log_debug(message, key=debugKey):
    print("DEBUG:", message, file=sys.stdout)
    redisClient.lpush('logging', f"{debugKey}:{message}")


def log_info(message, key=infoKey):
    print("INFO:", message, file=sys.stdout)
    redisClient.lpush('logging', f"{infoKey}:{message}")


def ensure_buckets():
    for bucket in (QUEUE_BUCKET, OUTPUT_BUCKET):
        if not minioClient.bucket_exists(bucket):
            minioClient.make_bucket(bucket)


def normalize_track(track):
    track = track.replace(".mp3", "")
    if track == "base":      # the README says "base", Demucs names it "bass"
        track = "bass"
    return track


@app.route('/', methods=['GET'])
def hello():
    return '<h1> Music Separation Server</h1><p> Use a valid endpoint </p>'


@app.route('/apiv1/separate', methods=['POST'])
def separate():
    data = request.get_json(force=True)
    mp3_bytes = base64.b64decode(data['mp3'])
    songhash = hashlib.sha224(mp3_bytes).hexdigest()

    ensure_buckets()
    minioClient.put_object(QUEUE_BUCKET, f"{songhash}.mp3",
                           io.BytesIO(mp3_bytes), len(mp3_bytes))

    message = {
        "hash": songhash,
        "callback": data.get("callback"),
        "model": data.get("model"),
    }
    redisClient.lpush("toWorker", json.dumps(message))
    log_info(f"Enqueued {songhash} ({len(mp3_bytes)} bytes)")

    return jsonify({"hash": songhash, "reason": "Song enqueued for separation"})


@app.route('/apiv1/queue', methods=['GET'])
def queue():
    items = redisClient.lrange("toWorker", 0, -1)
    hashes = [json.loads(i)["hash"] for i in items]
    return jsonify({"queue": hashes})


@app.route('/apiv1/track/<songhash>/<track>', methods=['GET'])
def get_track(songhash, track):
    track = normalize_track(track)
    try:
        obj = minioClient.get_object(OUTPUT_BUCKET, f"{songhash}-{track}.mp3")
        content = obj.read()
        obj.close()
        obj.release_conn()
    except Exception as e:
        log_debug(f"track not found {songhash}-{track}: {e}")
        return jsonify({"error": "track not found"}), 404
    return Response(content, mimetype="audio/mpeg")


@app.route('/apiv1/remove/<songhash>/<track>', methods=['GET', 'DELETE'])
def remove_track(songhash, track):
    track = normalize_track(track)
    try:
        minioClient.remove_object(OUTPUT_BUCKET, f"{songhash}-{track}.mp3")
    except Exception as e:
        log_debug(f"remove failed {songhash}-{track}: {e}")
        return jsonify({"error": "could not remove"}), 404
    return jsonify({"removed": f"{songhash}-{track}.mp3"})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
