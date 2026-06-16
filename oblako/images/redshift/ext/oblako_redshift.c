/*
 * oblako_redshift — make a stock PostgreSQL look enough like Amazon Redshift
 * that Amazon's redshift_connector driver connects natively (no wire proxy).
 *
 * Loaded via shared_preload_libraries. In _PG_init it:
 *   1. registers the Redshift-only startup parameters redshift_connector sends
 *      (client_protocol_version, driver_version, os_version,
 *      driver_discovery_version) plus query_group, as accepted no-op GUCs — so
 *      the connection handshake is not rejected;
 *   2. reports server_version = 8.0.2 (Redshift's PostgreSQL version), so the
 *      driver parses it cleanly and takes its genuine Redshift code path.
 */
#include "postgres.h"
#include "fmgr.h"
#include "utils/guc.h"

PG_MODULE_MAGIC;

void _PG_init(void);

/* one backing string per no-op GUC (never read; the GUC just has to exist) */
static char *guc_store[8];

static const char *const redshift_gucs[] = {
	"client_protocol_version",
	"driver_version",
	"os_version",
	"driver_discovery_version",
	"query_group",
	NULL,
};

void
_PG_init(void)
{
	int i;

	for (i = 0; redshift_gucs[i] != NULL; i++)
		DefineCustomStringVariable(redshift_gucs[i],
								   "Amazon Redshift compatibility shim (no-op).",
								   NULL,
								   &guc_store[i],
								   "",
								   PGC_USERSET,
								   GUC_NO_SHOW_ALL,
								   NULL, NULL, NULL);

	/* Impersonate Redshift's reported PostgreSQL version. */
	SetConfigOption("server_version", "8.0.2", PGC_INTERNAL, PGC_S_OVERRIDE);
}
