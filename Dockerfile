FROM python:3.12-slim
WORKDIR /app
COPY bigshort ./bigshort
RUN useradd --uid 10001 --create-home trader \
    && mkdir /data /hunter \
    && chown trader /data /hunter
USER trader
CMD ["python", "-m", "bigshort.cli", "--db", "/data/paper.sqlite", "paper", "--kill-file", "/data/STOP"]
