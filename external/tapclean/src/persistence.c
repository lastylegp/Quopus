/**
 *	@file 	persistence.c
 *	@brief	A module to reuse discovered loader parameters in subsequent scans.
 *
 *	This module persists discovered loader parameters in subsequent
 *  invokations of the application.
 */

#include <stdio.h>
#include <string.h>
#ifdef _MSC_VER
#include <windows.h>
#define usleep	Sleep
#else
#include <unistd.h>
#endif
#ifdef WIN32
#include <direct.h>
#endif

#include "mydefs.h"
#include "persistence.h"

static const char *persistentstore = "persistence.ini";
static const char *persistencetmp  = "persistence.tmp";

static int wait_for_store_update_to_finish ()
{
	FILE *pFile;
	int   retries;

	for (retries = 0;;) {
		pFile = fopen (persistencetmp, "r");
		if (pFile == NULL)
			break;
		fclose (pFile);

		retries++;
		if (retries >= 3)
			return 1;

		usleep (1000);
	}

	return 0;
}

int persistence_load_loader_parameters (void)
{
	FILE *pFile;
	char  readbuffer[64];
	size_t i;

	chdir (exedir);

	if (wait_for_store_update_to_finish ())
		return PERS_LOCK_TIMEOUT;

	pFile = fopen (persistentstore, "r");
	if (pFile == NULL)
		return PERS_OPEN_ERROR;

	while (fgets (readbuffer, 64, pFile) != NULL) {
		int  version;
		char loader[32];
		int  en, tp, sp, mp, lp, pv, sv;

		if (sscanf (readbuffer,
				"version = %d",
				&version) == 1) {
			if (version != 1) {
				fclose (pFile);
				return PERS_UNSUPPORTED_VERSION;
			}
		}

		if (sscanf (readbuffer,
				"%s = %d %d %d %d %d %d %d",
				loader,
				&en,
				&tp,
				&sp,
				&mp,
				&lp,
				&pv,
				&sv) == 8) {

			for (i = 0; i < strlen (loader); i++) {
				if (loader[i] == '~')
					loader[i] = ' ';
			}

			for (i = CBM_HEAD; ft[i].name[0]; i++) {
				if (strncmp (loader, ft[i].name, strlen (ft[i].name)) == 0) {
					ft[i].en = en;
					ft[i].tp = tp;
					ft[i].sp = sp;
					ft[i].mp = mp;
					ft[i].lp = lp;
					ft[i].pv = pv;
					ft[i].sv = sv;

					break;
				}
			}
		}
	}

	fclose (pFile);

	return PERS_OK;
}

int persistence_save_loader_parameters (void)
{
	FILE *pFile;
	size_t i;

	chdir (exedir);

	if (wait_for_store_update_to_finish ())
		return PERS_LOCK_TIMEOUT;

	pFile = fopen (persistencetmp, "w+");
	if (pFile == NULL)
		return PERS_OPEN_ERROR;

	fputs ("# TAPClean persistence file - do NOT edit\n", pFile);
	fputs ("[persistence]\n", pFile);
	fputs ("version = 1\n", pFile);
	fputs ("[loader_values]\n", pFile);

	for (i = CBM_HEAD; ft[i].name[0]; i++) {
		char writebuffer[64];
		size_t j;

		sprintf (writebuffer,
				"%s = %d %d %d %d %d %d %d\n",
				ft[i].name,
				ft[i].en,
				ft[i].tp,
				ft[i].sp,
				ft[i].mp,
				ft[i].lp,
				ft[i].pv,
				ft[i].sv);

		for (j = 0; j < strlen (ft[i].name); j++) {
			if (writebuffer[j] == ' ')
				writebuffer[j] = '~';
		}

		fputs (writebuffer, pFile);
	}

	fclose (pFile);

	unlink (persistentstore);

	if (rename (persistencetmp, persistentstore))
		return PERS_IO_ERROR;

	return PERS_OK;
}
