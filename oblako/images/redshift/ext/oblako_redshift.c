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
#include <stdlib.h>
#include <string.h>

PG_MODULE_MAGIC;

void _PG_init(void);

/* one backing string per no-op GUC (never read; the GUC just has to exist) */
static char *guc_store[8];

/*
 * Redshift's enable_case_sensitive_identifier: off by default, as on Redshift.
 * Tools read it (awswrangler's to_sql(add_new_columns=True) runs SHOW) and set
 * it; PostgreSQL already keeps quoted identifiers' case, so it changes nothing.
 */
static bool case_sensitive_identifier = false;

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
	const char *spoof;
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

	DefineCustomBoolVariable("enable_case_sensitive_identifier",
							 "Amazon Redshift compatibility: case-sensitive identifiers.",
							 NULL,
							 &case_sensitive_identifier,
							 false,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	/*
	 * Impersonate Redshift's reported PostgreSQL version so the driver parses it
	 * cleanly. OBLAKO_SPOOF_SERVER_VERSION overrides the value; setting it to "off"
	 * (or empty) disables the in-engine spoof. That is required on the Citus MPP
	 * variant, where a fake server_version breaks CREATE EXTENSION citus; there the
	 * wire proxy presents the Redshift version to the client instead, and the
	 * engine keeps its real version for Citus.
	 */
	spoof = getenv("OBLAKO_SPOOF_SERVER_VERSION");
	if (spoof == NULL)
		spoof = "8.0.2";
	if (spoof[0] != '\0' && strcmp(spoof, "off") != 0)
		SetConfigOption("server_version", spoof, PGC_INTERNAL, PGC_S_OVERRIDE);
}
