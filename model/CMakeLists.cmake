build_exec(
  EXECNAME fack-paper
  SOURCE_FILES fack-paper.cc fack-model.cc
  HEADER_FILES fack-model.h queue-probe.h
  LIBRARIES_TO_LINK ${libcore} ${libnetwork} ${libpoint-to-point}
  EXECUTABLE_DIRECTORY_PATH ${CMAKE_OUTPUT_DIRECTORY}/scratch/fack-paper
)
