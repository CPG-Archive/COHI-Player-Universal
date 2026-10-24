####################################################
# COHI-Player-Universal
# V1.0.0, 24.10.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

# Always run 'make' directly on the target device - never copy compiled
# binaries between different Raspberry Pi models (-march=native).

CXX      := g++
CXXFLAGS := -O3 -march=native -ffast-math
LDLIBS   := -lm -lpthread

TARGETS := cohi_native_upconverter cohi_stemlab_live_upconverter

.PHONY: all clean help

all: $(TARGETS)
	@echo ""
	@echo "Fertig: $(TARGETS)"
	@echo "Hinweis: diese Binaerdateien sind NUR auf diesem Geraet lauffaehig"
	@echo "(siehe -march=native oben) - auf einem anderen Raspberry Pi bitte"
	@echo "dort 'make clean && make' erneut ausfuehren, statt die Dateien"
	@echo "zu kopieren."

cohi_native_upconverter: cohi_native_upconverter.cpp
	$(CXX) $(CXXFLAGS) -o $@ $< $(LDLIBS)

cohi_stemlab_live_upconverter: cohi_stemlab_live_upconverter.cpp
	$(CXX) $(CXXFLAGS) -o $@ $< $(LDLIBS)

clean:
	rm -f $(TARGETS)

help:
	@echo "Verfuegbare Ziele:"
	@echo "  make                              - beide C++-Tools kompilieren (Standard)"
	@echo "  make cohi_native_upconverter       - nur das FL2K-Tool kompilieren"
	@echo "  make cohi_stemlab_live_upconverter - nur das STEMlab-Tool kompilieren"
	@echo "  make clean                         - kompilierte Binaerdateien entfernen"
	@echo "  make help                          - diese Uebersicht anzeigen"
