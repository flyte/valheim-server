# Set the base image
FROM ubuntu:18.04

# Set environment variables
ENV USER vhserver
ENV HOME /home/vhserver
ENV SERVER_INSTALL_DIR /home/vhserver/vhserver
ENV APP_ID 896660

# Insert Steam prompt answers
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN echo steam steam/question select "I AGREE" | debconf-set-selections && \
    echo steam steam/license note '' | debconf-set-selections
SHELL ["/bin/bash", "-c"]

# Update the repository and install SteamCMD
ARG DEBIAN_FRONTEND=noninteractive
RUN dpkg --add-architecture i386 && \
    apt-get update -y && \
    apt-get install -y --no-install-recommends ca-certificates locales steamcmd && \
    rm -rf /var/lib/apt/lists/*

# Add unicode support
RUN locale-gen en_US.UTF-8
ENV LANG 'en_US.UTF-8'
ENV LANGUAGE 'en_US:en'

# Create symlink for executable
RUN ln -s /usr/games/steamcmd /usr/bin/steamcmd

RUN groupadd $USER && useradd -d $HOME -m -s /bin/bash -g $USER $USER 
WORKDIR $HOME
USER $USER

RUN steamcmd \
    +login anonymous \
    +force_install_dir $SERVER_INSTALL_DIR \
    +app_update $APP_ID \
    +quit

USER root

RUN apt-get update && apt-get -y install curl python3.8 python3.8-distutils
RUN curl https://bootstrap.pypa.io/get-pip.py | python3.8

USER $USER

RUN pip install --user python-a2s
COPY run_server.py $HOME/

CMD [ "python3.8", "run_server.py" ]