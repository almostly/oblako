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
 *      driver parses it cleanly and takes its genuine Redshift code path;
 *   3. enforces Redshift's ALTER and DROP privileges on tables, views and schemas
 *      (kept in pg_oblako.object_privileges): an ALTER or DROP by a user that
 *      holds the privilege but doesn't own the object runs as the object's owner.
 */
#include "postgres.h"
#include "fmgr.h"
#include "access/htup_details.h"
#include "access/xact.h"
#include "catalog/namespace.h"
#include "catalog/pg_class.h"
#include "catalog/pg_namespace.h"
#include "catalog/pg_type.h"
#include "executor/spi.h"
#include "miscadmin.h"
#include "nodes/parsenodes.h"
#include "tcop/utility.h"
#include "utils/acl.h"
#include "utils/builtins.h"
#include "utils/guc.h"
#include "utils/lsyscache.h"
#include "utils/syscache.h"
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

static ProcessUtility_hook_type prev_ProcessUtility = NULL;

/* an object an ALTER or DROP acts on: a relation or a schema, and its owner */
typedef struct Target
{
	const char *kind;			/* "relation" or "schema", as pg_oblako names them */
	Oid			oid;
	Oid			owner;
} Target;

static bool
relation_target(RangeVar *rv, Target *t)
{
	HeapTuple	tup;

	t->oid = RangeVarGetRelid(rv, NoLock, true);
	if (!OidIsValid(t->oid))
		return false;
	tup = SearchSysCache1(RELOID, ObjectIdGetDatum(t->oid));
	if (!HeapTupleIsValid(tup))
		return false;
	t->kind = "relation";
	t->owner = ((Form_pg_class) GETSTRUCT(tup))->relowner;
	ReleaseSysCache(tup);
	return true;
}

static bool
schema_target(const char *name, Target *t)
{
	HeapTuple	tup;

	t->oid = get_namespace_oid(name, true);
	if (!OidIsValid(t->oid))
		return false;
	tup = SearchSysCache1(NAMESPACEOID, ObjectIdGetDatum(t->oid));
	if (!HeapTupleIsValid(tup))
		return false;
	t->kind = "schema";
	t->owner = ((Form_pg_namespace) GETSTRUCT(tup))->nspowner;
	ReleaseSysCache(tup);
	return true;
}

/* whether the current user holds `privilege` on the target in pg_oblako */
static bool
holds_privilege(Target *t, const char *privilege)
{
	Oid			argtypes[4] = {TEXTOID, OIDOID, TEXTOID, NAMEOID};
	Datum		values[4];
	bool		held = false;

	if (!OidIsValid(get_namespace_oid("pg_oblako", true)))
		return false;
	values[0] = CStringGetTextDatum(t->kind);
	values[1] = ObjectIdGetDatum(t->oid);
	values[2] = CStringGetTextDatum(privilege);
	values[3] = DirectFunctionCall1(namein,
									CStringGetDatum(GetUserNameFromId(GetUserId(), false)));
	SPI_connect();
	if (SPI_execute("SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n"
					" ON n.oid = p.pronamespace WHERE n.nspname = 'pg_oblako'"
					" AND p.proname = 'holds_object_privilege'", true, 1) == SPI_OK_SELECT
		&& SPI_processed > 0
		&& SPI_execute_with_args("SELECT pg_oblako.holds_object_privilege($1, $2, $3, $4)",
								 4, argtypes, values, NULL, true, 1) == SPI_OK_SELECT
		&& SPI_processed > 0)
	{
		bool		isnull;
		Datum		d = SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 1,
									  &isnull);

		held = !isnull && DatumGetBool(d);
	}
	SPI_finish();
	return held;
}

/*
 * The owner to run the statement as: when the user doesn't own every object it
 * names but holds `privilege` on each one it doesn't, and those share one owner.
 * InvalidOid otherwise (PostgreSQL then checks ownership as usual).
 */
static Oid
owner_to_act_as(List *targets, const char *privilege)
{
	Oid			user = GetUserId();
	Oid			owner = InvalidOid;
	ListCell   *lc;

	foreach(lc, targets)
	{
		Target	   *t = (Target *) lfirst(lc);

		if (has_privs_of_role(user, t->owner))
			continue;
		if (!holds_privilege(t, privilege))
			return InvalidOid;
		if (OidIsValid(owner) && owner != t->owner)
			return InvalidOid;
		owner = t->owner;
	}
	return owner;
}

/* the objects an ALTER or DROP names, and which privilege it needs */
static List *
statement_targets(Node *stmt, const char **privilege)
{
	List	   *targets = NIL;
	Target	   *t = palloc(sizeof(Target));

	switch (nodeTag(stmt))
	{
		case T_AlterTableStmt:
			*privilege = "ALTER";
			if (relation_target(((AlterTableStmt *) stmt)->relation, t))
				targets = lappend(targets, t);
			break;
		case T_RenameStmt:
			{
				RenameStmt *r = (RenameStmt *) stmt;

				*privilege = "ALTER";
				if (r->renameType == OBJECT_SCHEMA)
				{
					if (schema_target(r->subname, t))
						targets = lappend(targets, t);
				}
				else if (r->relation != NULL && relation_target(r->relation, t))
					targets = lappend(targets, t);
				break;
			}
		case T_DropStmt:
			{
				DropStmt   *d = (DropStmt *) stmt;
				ListCell   *lc;

				*privilege = "DROP";
				if (d->removeType != OBJECT_TABLE && d->removeType != OBJECT_VIEW
					&& d->removeType != OBJECT_MATVIEW && d->removeType != OBJECT_SCHEMA)
					break;
				foreach(lc, d->objects)
				{
					t = palloc(sizeof(Target));
					if (d->removeType == OBJECT_SCHEMA
						? schema_target(strVal(lfirst(lc)), t)
						: relation_target(makeRangeVarFromNameList(lfirst(lc)), t))
						targets = lappend(targets, t);
				}
				break;
			}
		default:
			break;
	}
	return targets;
}

static void
oblako_ProcessUtility(PlannedStmt *pstmt, const char *queryString, bool readOnlyTree,
					  ProcessUtilityContext context, ParamListInfo params,
					  QueryEnvironment *queryEnv, DestReceiver *dest, QueryCompletion *qc)
{
	const char *privilege = NULL;
	Oid			owner = InvalidOid;
	List	   *targets = NIL;

	if (IsTransactionState())
		targets = statement_targets(pstmt->utilityStmt, &privilege);
	if (targets != NIL)
		owner = owner_to_act_as(targets, privilege);
	if (OidIsValid(owner))
	{
		Oid			save_userid;
		int			save_sec_context;

		GetUserIdAndSecContext(&save_userid, &save_sec_context);
		SetUserIdAndSecContext(owner, save_sec_context | SECURITY_LOCAL_USERID_CHANGE);
		PG_TRY();
		{
			(prev_ProcessUtility ? prev_ProcessUtility : standard_ProcessUtility)
				(pstmt, queryString, readOnlyTree, context, params, queryEnv, dest, qc);
		}
		PG_FINALLY();
		{
			SetUserIdAndSecContext(save_userid, save_sec_context);
		}
		PG_END_TRY();
		return;
	}
	(prev_ProcessUtility ? prev_ProcessUtility : standard_ProcessUtility)
		(pstmt, queryString, readOnlyTree, context, params, queryEnv, dest, qc);
}

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

	prev_ProcessUtility = ProcessUtility_hook;
	ProcessUtility_hook = oblako_ProcessUtility;

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
