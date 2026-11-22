cmake_minimum_required(VERSION 3.24)
project(SystemsPerformanceEngine VERSION 0.6.0 LANGUAGES CXX)

if(NOT UNIX)
    message(FATAL_ERROR "The initial engine supports POSIX platforms (macOS/Linux).")
endif()
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

option(SPE_ENABLE_SANITIZERS "Enable AddressSanitizer and UndefinedBehaviorSanitizer" OFF)
find_package(CURL 7.85 REQUIRED)

add_library(spe_options INTERFACE)
target_compile_features(spe_options INTERFACE cxx_std_20)
set(CMAKE_CXX_EXTENSIONS OFF)
if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
    target_compile_options(spe_options INTERFACE -Wall -Wextra -Wpedantic -Wconversion -Wshadow)
    if(SPE_ENABLE_SANITIZERS)
        target_compile_options(spe_options INTERFACE -fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer)
        target_link_options(spe_options INTERFACE -fsanitize=address,undefined)
    endif()
elseif(SPE_ENABLE_SANITIZERS)
    message(FATAL_ERROR "Sanitizers require Clang or GCC.")
endif()

find_package(Git QUIET)
set(SPE_REVISION "unknown")
if(Git_FOUND)
    execute_process(COMMAND "${GIT_EXECUTABLE}" rev-parse --short HEAD
        WORKING_DIRECTORY "${PROJECT_SOURCE_DIR}" OUTPUT_VARIABLE SPE_REVISION OUTPUT_STRIP_TRAILING_WHITESPACE)
    execute_process(COMMAND "${GIT_EXECUTABLE}" status --porcelain
        WORKING_DIRECTORY "${PROJECT_SOURCE_DIR}" OUTPUT_VARIABLE SPE_CHANGES OUTPUT_STRIP_TRAILING_WHITESPACE)
    if(SPE_CHANGES)
        string(APPEND SPE_REVISION "+dirty")
    endif()
endif()
string(TOUPPER "${CMAKE_BUILD_TYPE}" SPE_BUILD_TYPE_UPPER)
set(SPE_BUILD_FLAGS "${CMAKE_CXX_FLAGS} ${CMAKE_CXX_FLAGS_${SPE_BUILD_TYPE_UPPER}}")
if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
    string(APPEND SPE_BUILD_FLAGS " -std=c++20 -Wall -Wextra -Wpedantic -Wconversion -Wshadow")
    if(SPE_ENABLE_SANITIZERS)
        string(APPEND SPE_BUILD_FLAGS " -fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer")
    endif()
endif()
configure_file(src/build_info.hpp.in generated/spe/build_info.hpp @ONLY)

add_library(spe_core src/config.cpp src/config_json.cpp src/workload.cpp src/schedule.cpp src/engine.cpp src/verification.cpp src/result.cpp src/tls.cpp src/headers.cpp)
target_include_directories(spe_core PUBLIC include PRIVATE "${PROJECT_BINARY_DIR}/generated")
target_include_directories(spe_core SYSTEM PRIVATE third_party/nlohmann/include)
target_link_libraries(spe_core PUBLIC CURL::libcurl spe_options)
if(NOT APPLE)
    find_package(OpenSSL 1.1.1 REQUIRED COMPONENTS Crypto)
    target_link_libraries(spe_core PRIVATE OpenSSL::Crypto)
endif()
add_library(spe_comparison src/compare.cpp src/analyze.cpp)
target_include_directories(spe_comparison PUBLIC include)
target_include_directories(spe_comparison SYSTEM PRIVATE third_party/nlohmann/include)
target_link_libraries(spe_comparison PUBLIC spe_core)
add_executable(spe src/main.cpp)
target_link_libraries(spe PRIVATE spe_comparison)

include(CTest)
if(BUILD_TESTING)
    add_executable(spe_unit_tests tests/unit.cpp)
    target_link_libraries(spe_unit_tests PRIVATE spe_core)
    add_test(NAME unit COMMAND spe_unit_tests)
    find_package(Python3 3.10 REQUIRED COMPONENTS Interpreter)
    find_program(SPE_OPENSSL_PROGRAM openssl REQUIRED)
    add_test(NAME integration COMMAND "${Python3_EXECUTABLE}"
        "${PROJECT_SOURCE_DIR}/tests/run_suite.py" --suite integration --engine "$<TARGET_FILE:spe>"
        --artifacts "${PROJECT_BINARY_DIR}/test-results/managed")
    add_test(NAME mixed_workload COMMAND "${Python3_EXECUTABLE}"
        "${PROJECT_SOURCE_DIR}/tests/run_suite.py" --suite mixed_workload --engine "$<TARGET_FILE:spe>"
        --artifacts "${PROJECT_BINARY_DIR}/test-results/managed")
    add_test(NAME comparison COMMAND "${Python3_EXECUTABLE}"
        "${PROJECT_SOURCE_DIR}/tests/run_suite.py" --suite comparison --engine "$<TARGET_FILE:spe>"
        --artifacts "${PROJECT_BINARY_DIR}/test-results/managed")
    add_test(NAME https COMMAND "${Python3_EXECUTABLE}"
        "${PROJECT_SOURCE_DIR}/tests/run_suite.py" --suite https --engine "$<TARGET_FILE:spe>"
        --openssl "${SPE_OPENSSL_PROGRAM}" --artifacts "${PROJECT_BINARY_DIR}/test-results/managed")
    add_test(NAME headers COMMAND "${Python3_EXECUTABLE}"
        "${PROJECT_SOURCE_DIR}/tests/run_suite.py" --suite headers --engine "$<TARGET_FILE:spe>"
        --openssl "${SPE_OPENSSL_PROGRAM}" --artifacts "${PROJECT_BINARY_DIR}/test-results/managed")
    add_test(NAME methods COMMAND "${Python3_EXECUTABLE}"
        "${PROJECT_SOURCE_DIR}/tests/run_suite.py" --suite methods --engine "$<TARGET_FILE:spe>"
        --openssl "${SPE_OPENSSL_PROGRAM}" --artifacts "${PROJECT_BINARY_DIR}/test-results/managed")
    add_test(NAME paired_analysis COMMAND "${Python3_EXECUTABLE}"
        "${PROJECT_SOURCE_DIR}/tests/analysis.py" --engine "$<TARGET_FILE:spe>")
    set_tests_properties(paired_analysis PROPERTIES TIMEOUT 120 LABELS "comparison;network;integration")
    add_test(NAME response_verification COMMAND "${Python3_EXECUTABLE}"
        "${PROJECT_SOURCE_DIR}/tests/verification.py" --engine "$<TARGET_FILE:spe>" --openssl "${SPE_OPENSSL_PROGRAM}")
    set_tests_properties(response_verification PROPERTIES TIMEOUT 120 LABELS "tls;network;integration")
    add_test(NAME retention_unit COMMAND "${Python3_EXECUTABLE}"
        "${PROJECT_SOURCE_DIR}/tests/test_run_suite.py")
    add_test(NAME restoration_unit COMMAND "${Python3_EXECUTABLE}"
        "${PROJECT_SOURCE_DIR}/tests/test_restore_profiles.py")
    add_test(NAME experiment_unit COMMAND "${Python3_EXECUTABLE}"
        "${PROJECT_SOURCE_DIR}/tests/test_compare_petclinic.py")
    add_test(NAME target_unit COMMAND "${Python3_EXECUTABLE}"
        "${PROJECT_SOURCE_DIR}/tests/test_prepare_petclinic.py")
    set_tests_properties(integration mixed_workload PROPERTIES TIMEOUT 120 LABELS "network;integration")
    set_tests_properties(comparison PROPERTIES TIMEOUT 120 LABELS "comparison;network;integration")
    set_tests_properties(https PROPERTIES TIMEOUT 180 LABELS "tls;network;integration")
    set_tests_properties(headers PROPERTIES TIMEOUT 180 LABELS "headers;tls;network;integration")
    set_tests_properties(methods PROPERTIES TIMEOUT 180 LABELS "methods;tls;network;integration")
endif()
