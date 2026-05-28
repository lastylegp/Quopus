/*
 * tap2audio.c
 *
 * Part of project "Final TAP".
 *
 * A Commodore 64 tape remastering and data extraction utility.
 *
 * (C) 2001-2006 Stewart Wilson, Subchrist Software.
 *
 *
 *
 * This program is free software; you can redistribute it and/or modify it under
 * the terms of the GNU General Public License as published by the Free Software
 * Foundation; either version 2 of the License, or (at your option) any later
 * version.
 *
 * This program is distributed in the hope that it will be useful, but WITHOUT ANY
 * WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
 * PARTICULAR PURPOSE. See the GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License along with
 * this program; if not, write to the Free Software Foundation, Inc., 51 Franklin
 * St, Fifth Floor, Boston, MA 02110-1301 USA
 *
 *
 * Notes:-
 *
 * Contains functions for converting and saving a TAP file as audio files.
 *
 * Currently supports...
 *
 * AU  (Sun Microsystems)
 * WAV (Microsoft)
 *
 */

#include "tap2audio.h"

const char tap2audio_auoutname[] =	"out.au";
const char tap2audio_wavoutname[] =	"out.wav";

/**
 *	Internal usage functions
 */

/*
 * rather than using the very slow method of using fputc() for every sample in the file,
 * I write to a memory buffer, When it's full it (quickly) dumps its contents to
 * the disk file and resets its position pointer (bp) to the beginning again.
 * the function below implements all this, "amp" is the amplitude of the sample being written,
 * 'finished' is just a flag to tell it to dump only up to the current position of bp, ie.
 * the tail-end of the file, which will be BUFSZ bytes or less.
 *
 * Returns the number of samples successfully written, 0 or 1.
 */

#include <math.h>
#include <stdlib.h>
#include <stdio.h>

static int s_out(unsigned char amp, int finished, FILE *filep, unsigned long *bp, char *outbuf)
{
	if (!finished) {
		outbuf[(*bp)++] = amp;
		if (*bp == BUFSZ) {				/* buffer full? */
			fwrite(outbuf, 1, BUFSZ, filep);	/* add full buffer to the file */
			*bp = 0;
		}
		return 1;

	/* push all remaining valid bytes to the file if 'finished' is true.. */

	} else {
		fwrite(outbuf, 1, *bp, filep);
		return 0;
	}
}

/*
 * write out square wave data of amplitude 'amp' and length 'len'.
 * if parameter 'signd' is set (1) the wave data will be in a signed format
 * else unsigned.
 */

static int drawwavesquare(int len, int amp, char signd, FILE *filep, unsigned long *bp, char *outbuf)
{
	int x, y, off;
	int samples = 0;

	if (signd)
		off = 0;
	else
		off = 128;

	for (x = 0; x < len; x++) {
		if (x < (len >> 1))	/* peak first */
			y = amp;
		else
			y =- amp;
		samples += s_out(y + off, 0, filep, bp, outbuf);
	}

	return samples;
}

/*
 * write out sine wave data of amplitude 'amp' and length 'len'.
 * if parameter 'signd' is set (1) the wave data will be in a signed format
 * else unsigned.
 */

static int drawwavesine(int len, int amp, char signd, FILE *filep, unsigned long *bp, char *outbuf)
{
	int x, y, off;
	float deg, inc;
	float rads = (float) (180. / 3.141592654);
	int samples = 0;

	if (signd)
		off = 0;
	else
		off = 128;

	for (x = 0; x < len; x++) {
		inc = (float)360 / len;
		deg = (x * inc);	/* +180 makes the wave peak first */
		y = (int) (amp * sin(deg / rads));
		samples += s_out(y + off, 0, filep, bp, outbuf);
	}

	return samples;
}


/**
 *	Public functions
 */


/*
 * Write out the TAP as a Sun AU file...
 * 'sine' is boolean, if true then the wave is written as sine.
 */

int tap2audio_au_write(unsigned char *tap, int taplen, const char *filename, char sine)
{
	int b, tapversion;
	long i, lv, ccnt;
	double fratio = FREQ/985248.0;
	unsigned int dsize, totalsamples = 0;
	FILE *fp;
	unsigned long bpos = 0;
	char *outbuf;

	/* ".snd", DataOff, DataSize, Format, SampleRate(44khz), Channels */

	char auhd[64] = { 46, 115, 110, 100, 0, 0, 0, 24, 255, 255, 255, 255,
			  0, 0, 0, 2, 0, 0, 172, 68, 0, 0, 0, 1 };

	tapversion = tap[12];

	fp = fopen(filename, "w+b");		/* create output file... */
	if (fp == NULL)
		return -1;

	outbuf = (char*)malloc(BUFSZ);		/* create output buffer... */
	if (outbuf == NULL)
		return -1;

	/* write out a dummy header, proper one needs to go in after. */

	for (i = 0; i < AUHDSZ; i++)
		fputc(0, fp);

	/* write out the waveform data... */

	for (i = 20; i < taplen; i++) {
		b = tap[i];

		/* write TAP v0 pause... */

		if (b == 0 && tapversion == 0) {
			lv = (long) floor(20000 * fratio);
			if (sine)
				totalsamples += drawwavesine(lv, 0, 1, fp, &bpos, outbuf);
			else
				totalsamples += drawwavesquare(lv, 0, 1, fp, &bpos, outbuf);
		}

		/* write TAP v1 pause... */

		if (b == 0 && tapversion == 1) {
			ccnt = (tap[i + 1]) + (tap[i + 2] << 8)+ (tap[i + 3] << 16);
			i += 3;
			lv = (long) floor(ccnt * fratio);
			if (sine)
				totalsamples += drawwavesine(lv, 0, 1, fp, &bpos, outbuf);
			else
				totalsamples += drawwavesquare(lv, 0, 1, fp, &bpos, outbuf);
		}

		/* write out non-pause... */

		if (b != 0) {
			lv = (long) floor((b * 8) * fratio);
			if (sine)
				totalsamples += drawwavesine(lv, 127, 1, fp, &bpos, outbuf);
			else
				totalsamples += drawwavesquare(lv, 127, 1, fp, &bpos, outbuf);
		}
	}

	totalsamples += s_out(0, 1, fp, &bpos, outbuf);	/* add all valid remaining buffer bytes. */

	dsize = totalsamples;

	/* write data size to header array... */

	for (i = 0; i < 4; i++) {
		b = (dsize &(0xFF << ((3 - i) * 8))) >> ((3 - i) * 8);
		auhd[8 + i] = b;
	}

	/* write out the header and clean up... */

	rewind(fp);
	for (i = 0; i < AUHDSZ; i++)
		fputc(auhd[i], fp);

	fclose(fp);
	free(outbuf);

	return 0;
}

/*
 * Write out the TAP as a Sun AU file...
 *'sine' is boolean, if true then the wave is written as sine.
 */

int tap2audio_wav_write(unsigned char *tap, int taplen, const char *filename, char sine)
{
	long i, lv, ccnt;
	int b, tapversion;
	FILE *fp;
	unsigned long bpos = 0;
	char *outbuf;
	double fratio = FREQ / 985248.0;
	unsigned int dsize, totalsamples = 0;
	char wavhd[WAVHDSZ] = { 82, 73, 70, 70, 0, 0, 0, 0, 87, 65, 86, 69,
				102, 109, 116, 32, 16, 0, 0, 0, 1, 0, 1, 0,
				68, 172, 0, 0, 68, 172, 0, 0, 1, 0, 8, 0,
				100, 97, 116, 97, 0, 0, 0, 0 };

	tapversion = tap[12];

	fp = fopen(filename, "w+b");	/* create output file... */
	if (fp == NULL)
		return - 1;

	outbuf = (char*)malloc(BUFSZ);	/* create output buffer... */
	if (outbuf == NULL)
		return - 1;

	/* write out a dummy header, proper one needs to go in after. */

	for (i = 0; i < WAVHDSZ; i++)
		fputc(0, fp);

	/* write out the waveform data... */

	for(i = 20; i < taplen; i++) {
		b = tap[i];

		/* write TAP v0 pause... */

		if (b == 0 && tapversion == 0) {
			lv = (long) floor(20000 * fratio);
			if (sine)
				totalsamples += drawwavesine(lv, 0, 0, fp, &bpos, outbuf);
			else
				totalsamples += drawwavesquare(lv, 0, 0, fp, &bpos, outbuf);
		}

		/* write TAP v1 pause... */

		if (b == 0 && tapversion == 1) {
			ccnt = (tap[i + 1]) + (tap[i + 2] << 8) + (tap[i + 3] << 16);
			i += 3;
			lv = (long) floor(ccnt * fratio);
			if (sine)
				totalsamples += drawwavesine(lv, 0, 0, fp, &bpos, outbuf);
			else
				totalsamples += drawwavesquare(lv, 0, 0, fp, &bpos, outbuf);
		}

		/* write out non-pause... */

		if (b != 0) {
			lv = (long) floor((b * 8) * fratio);
			if (sine)
				totalsamples += drawwavesine(lv, 127, 0, fp, &bpos, outbuf);
			else
				totalsamples += drawwavesquare(lv, 127, 0, fp, &bpos, outbuf);
		}
	}

	totalsamples += s_out(0, 1, fp, &bpos, outbuf);	/* add all valid remaining buffer bytes. */

	/* write FileLen-8 to wav header... */

	dsize = totalsamples + WAVHDSZ - 8;
	for (i = 0; i < 4; i++) {
		b = (dsize & (0xFF << (i * 8))) >> (i * 8);
		wavhd[4 + i] = b;
	}

	/* write DataChunkLen to wav header... */

	dsize = totalsamples;
	for (i = 0; i < 4; i++) {
		b = ((dsize & (0xFF << (i * 8)))) >> (i * 8);
			wavhd[40 + i] = b;
		}

	/* write header array to file and clean up... */

	rewind(fp);
	for (i = 0; i < WAVHDSZ; i++)
		fputc(wavhd[i], fp);

	fclose(fp);
	free(outbuf);

	return 0;
}
