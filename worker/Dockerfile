FROM xserrat/facebook-demucs:latest

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN /bin/bash --login -c "pip3 install --no-cache-dir minio redis requests"

COPY worker-server.py .

# The base image's entrypoint is /bin/bash --login -c, so CMD is one shell string
CMD ["python3 /app/worker-server.py"]
