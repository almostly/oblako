# License

oblako is **open core**. This repository is the open-source edition, licensed
under the **Apache License 2.0**. Commercial Pro and hosted editions are offered
separately under their own terms.

## What that means for you

- Use it freely for local development, testing, and CI, in commercial and
  non-commercial settings alike.
- The Apache-2.0 license includes an explicit patent grant, which matters because
  oblako reimplements AWS API behavior.
- oblako lives in an Apache-licensed ecosystem (moto, boto3, Trino, S3Proxy all
  use Apache-2.0), so it composes with them without license friction.

## Third-party engines

oblako is a thin orchestration layer: its value is the topology it wires around
real engines, not the engines themselves. It **pulls** those engines at runtime
(as separate containers or processes) rather than redistributing them, so each
keeps its own license. The full inventory lives in `THIRD_PARTY_LICENSES.md` in
the repository.

Two of them carry terms worth knowing if you build a commercial product on top:

- **Citus** (the optional Redshift MPP cluster variant) is **AGPL-3.0**. The
  default single-node Redshift engine builds from PostgreSQL and is unaffected.
  The AGPL terms attach only if you build and offer the Citus-based cluster image
  as a network service.
- **DynamoDB Local** is under the **Amazon Software License**: free to use, but
  with restrictions on redistribution.

oblako's own source is Apache-2.0 regardless of which engines you run; these notes
are about the engines you pull, not about oblako.

None of this is legal advice. If you plan to redistribute oblako as part of a
commercial offering, review the flagged components with counsel first.
