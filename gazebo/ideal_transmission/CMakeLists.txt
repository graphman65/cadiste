cmake_minimum_required(VERSION 3.16)
project(cadiste_ideal_transmission LANGUAGES CXX)

# Jazzy vendors provide Harmonic headers, dependencies and CMake search paths.
find_package(gz_sim_vendor REQUIRED)
find_package(gz_transport_vendor REQUIRED)
find_package(gz_plugin_vendor REQUIRED)
find_package(gz-sim8 REQUIRED)
find_package(gz-transport13 REQUIRED)
find_package(gz-plugin2 REQUIRED COMPONENTS register)

add_library(cadiste-ideal-transmission-system SHARED IdealTransmission.cc)
target_compile_features(cadiste-ideal-transmission-system PRIVATE cxx_std_17)
target_link_libraries(cadiste-ideal-transmission-system PRIVATE
  gz-sim8::gz-sim8
  gz-transport13::gz-transport13
  gz-plugin2::register)
install(TARGETS cadiste-ideal-transmission-system LIBRARY DESTINATION lib)
