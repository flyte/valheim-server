FROM ubuntu:18.04

ENV USER vhserver
ENV SERVER_INSTALL_DIR /home/vhserver/vhserver
ENV APP_ID 896660
ENV PYTHONUNBUFFERED 1
ENV DEBIAN_FRONTEND=noninteractive

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN echo steam steam/question select "I AGREE" | debconf-set-selections && \
    echo steam steam/license note '' | debconf-set-selections
SHELL ["/bin/bash", "-c"]

RUN dpkg --add-architecture i386 && \
    apt-get update -y && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        locales \
        curl \
        python3.8 \
        python3.8-distutils \
        steamcmd && \
    rm -rf /var/lib/apt/lists/* && \
    curl https://bootstrap.pypa.io/get-pip.py | python3.8

RUN locale-gen en_US.UTF-8
ENV LANG 'en_US.UTF-8'
ENV LANGUAGE 'en_US:en'

RUN ln -s /usr/games/steamcmd /usr/bin/steamcmd && \
    ln -s /usr/bin/python3.8 /usr/bin/python

ENV HOME /home/vhserver
RUN groupadd $USER && \
    useradd -d $HOME -m -s /bin/bash -g $USER $USER

WORKDIR $HOME
USER $USER

RUN steamcmd \
    +login anonymous \
    +force_install_dir $SERVER_INSTALL_DIR \
    +app_update $APP_ID \
    +quit

COPY --chown=${USER}:${USER} requirements.txt ./
RUN pip install --user -r requirements.txt

COPY --chown=${USER}:${USER} run_server.py run_server_trio.py utils.py ./
CMD [ "python3.8", "run_server_trio.py" ]
