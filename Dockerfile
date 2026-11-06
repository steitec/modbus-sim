# modbus-sim in a container: by default three profiles after real devices behind
# one Modbus TCP port (Eastron SDM630 on unit 1, Omron E5CC on unit 2, SunSpec
# inverter on unit 3). Mount your own bench file over /bench/bench.yaml.
#
# The control channel listens on all interfaces inside the container, because
# nothing could reach it otherwise. It has no authentication, so publish it on
# the host's loopback only.
#
#   docker run --rm -p 5020:5020 -p 127.0.0.1:5021:5021 ghcr.io/steitec/modbus-sim
#   docker run --rm -p 502:5020 ghcr.io/steitec/modbus-sim
#   docker run --rm -p 5020:5020 -v ./bench.yaml:/bench/bench.yaml ghcr.io/steitec/modbus-sim
FROM python:3.13-slim
LABEL org.opencontainers.image.source=https://github.com/steitec/modbus-sim \
      org.opencontainers.image.description="Simulated Modbus devices with behaviour and fault injection over Modbus TCP" \
      org.opencontainers.image.licenses=MIT

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /src

COPY examples/real-devices.yaml /bench/bench.yaml
RUN useradd --create-home --uid 10001 modbus
USER modbus
WORKDIR /bench

EXPOSE 5020 5021
# Healthy once the Modbus TCP port accepts connections (the control channel may be off
# with --no-control). A service container in GitHub Actions waits for this.
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --start-interval=1s --retries=3 \
    CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 5020), 2).close()"]
ENTRYPOINT ["modbus-sim"]
CMD ["serve", "--bench", "/bench/bench.yaml", "--host", "0.0.0.0", "--control-host", "0.0.0.0"]
