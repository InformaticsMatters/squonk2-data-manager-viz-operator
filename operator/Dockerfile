FROM python:3.14.2-slim

# Labels
LABEL maintainer='Alan Christie <achristie@informaticsmatters.com>'

# Force the binary layer of the stdout and stderr streams
# to be unbuffered
ENV PYTHONUNBUFFERED=1
# 'kopf run' loads handlers.py by path, so make its sibling modules importable.
ENV PYTHONPATH=/src

COPY requirements.txt ./
RUN pip install -r requirements.txt

WORKDIR /src
COPY handlers.py datacentre.py /src/
COPY entrypoint.sh /src/

CMD ["./entrypoint.sh"]
