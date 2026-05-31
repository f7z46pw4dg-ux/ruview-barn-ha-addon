ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN pip3 install --no-cache-dir paho-mqtt==2.1.0

COPY ruview-ha-bridge.py /usr/bin/ruview-ha-bridge.py
COPY run.sh /run.sh

RUN chmod a+x /usr/bin/ruview-ha-bridge.py /run.sh

CMD ["/run.sh"]
