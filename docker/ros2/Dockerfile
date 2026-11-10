FROM ros:jazzy-ros-base

RUN apt-get update \
    && apt-get install -y --no-install-recommends ros-jazzy-ros-gz \
    && rm -rf /var/lib/apt/lists/*

COPY ros/cadiste_ros/ /opt/cadiste_ros/
COPY gazebo/ideal_transmission/ /tmp/cadiste_ideal_transmission/
RUN . /opt/ros/jazzy/setup.sh \
    && cmake -S /tmp/cadiste_ideal_transmission -B /tmp/cadiste_ideal_transmission/build \
       -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/cadiste_ros \
    && cmake --build /tmp/cadiste_ideal_transmission/build --parallel 2 \
    && cmake --install /tmp/cadiste_ideal_transmission/build \
    && rm -rf /tmp/cadiste_ideal_transmission
ENV AMENT_PREFIX_PATH=/opt/cadiste_ros:/opt/ros/jazzy
ENV GZ_SIM_SYSTEM_PLUGIN_PATH=/opt/cadiste_ros/lib
