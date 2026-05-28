# Project: tapclean

CPP  = g++
CC   = gcc
INCS = 
CXXINCS = 
CFLAGS = $(INCS)  
CXXFLAGS = $(CXXINCS)  
LIBS = -lm  
BIN  = tapclean

OBJ  =	main.o clean.o crc32.o batchscan.o database.o dc2nconv.o \
	filesearch.o loader_id.o skewadapt.o persistence.o \
	scanners/wildload.o scanners/t108DE0A5.o scanners/accolade.o \
	scanners/aces.o scanners/aliensyndrome.o scanners/alternativewg.o \
	scanners/anirog.o scanners/atlantis.o scanners/audiogenic.o \
	scanners/bleep_f1.o scanners/bleep_f2.o scanners/burner.o \
	scanners/burner_var.o scanners/c64tape.o scanners/megasave.o \
	scanners/cyberload_f1.o scanners/cyberload_f2.o \
	scanners/cyberload_f3.o scanners/cyberload_f4.o scanners/enigma.o \
	scanners/firebird.o scanners/flashload.o scanners/freeload.o \
	scanners/freezemachine.o scanners/gyrospeed.o scanners/hitec.o scanners/hitload.o \
	scanners/ikloader.o scanners/jetload.o scanners/microload.o \
	scanners/nova_f1.o scanners/nova_f2.o scanners/ocean.o \
	scanners/oceannew1.o scanners/oceannew2.o scanners/oceannew3.o \
	scanners/oceannew4.o scanners/odeload.o scanners/palace_f1.o \
	scanners/palace_f2.o scanners/pause.o scanners/pavloda.o \
	scanners/rackit.o scanners/rainbowarts_f1.o scanners/rainbowarts_f2.o \
	scanners/rasterload.o scanners/seuck.o scanners/snakeload50.o \
	scanners/snakeload51.o scanners/superpav.o \
	scanners/supertape.o scanners/tdi.o scanners/tdi_f2.o \
	scanners/trilogic.o scanners/turbotape.o scanners/biturbo.o \
	scanners/turrican.o scanners/usgold.o scanners/virgin.o \
	scanners/visiload.o scanners/actionreplay.o scanners/ashdave.o \
	scanners/freeslow.o scanners/goforgold.o scanners/jiffyload.o \
	scanners/fftape.o scanners/tes.o scanners/tequila.o \
	scanners/graphicadventurecreator.o scanners/chuckie_egg.o \
	scanners/alternativedk.o scanners/powerload.o scanners/gremlin_f1.o \
	scanners/gremlin_f2.o scanners/amaction.o scanners/creatures.o \
	scanners/rainbowislands.o scanners/easytape.o scanners/turbo220.o \
	scanners/creativesparks.o scanners/ddesign.o scanners/glass.o \
	scanners/turbotape_526.o scanners/microload_var.o scanners/lexpeed.o \
	scanners/mms.o scanners/gremlin_gbh.o scanners/lk_avalon.o \
	scanners/turbotape_263.o scanners/msx.o tap2audio.o

all: $(BIN)

$(BIN): $(OBJ)
	$(CC) $^ -o $@ $(LIBS)

clean:
	test -z "$(BIN)" || rm -f $(BIN)
	rm -f *~ *.o scanners/*~ scanners/*.o
