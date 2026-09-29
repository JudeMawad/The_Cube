.RECIPEPREFIX := >

MATRIX_DIR := build/rgbmatrix
MATRIX_LIB := $(MATRIX_DIR)/lib/librgbmatrix.a

CXX := g++
CXXFLAGS := -O3 -std=c++17 -Wall -Wextra -I$(MATRIX_DIR)/include
LIBS := $(MATRIX_LIB) -lrt -lm -lpthread

# Keep compilation separate from the executable used by the live service.
TARGET := build/cube-display
SOURCE := cube-display.cc content-mask.cc content-renderer.cc dissolve-transition.cc animation-asset.cc apps/tetris-clock.cc apps/weather.cc apps/animation.cc apps/music.cc
HEADERS := display-control.h display-frame.h display-runtime.h display-app-registry.h carousel-scheduler.h display-compositor.h content-mask.h content-renderer.h content-control.h dissolve-transition.h pixel-font.h weather-state.h music-state.h animation-asset.h apps/tetris-clock.h apps/tetris-clock-geometry.h apps/weather.h apps/animation.h apps/music.h

all: $(TARGET)

$(MATRIX_LIB): FORCE
>python3 prepare-matrix.py
>$(MAKE) -C $(MATRIX_DIR)/lib librgbmatrix.a

$(TARGET): $(SOURCE) $(HEADERS) $(MATRIX_LIB) Makefile
>$(CXX) $(CXXFLAGS) $(SOURCE) $(LIBS) -o $(TARGET).tmp
>mv $(TARGET).tmp $(TARGET)

clean:
>rm -f $(TARGET)

.PHONY: all clean FORCE
