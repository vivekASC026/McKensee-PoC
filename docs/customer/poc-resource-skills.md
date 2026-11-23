# SDM PoC at a new customer: the resource's skills

For this scope (2026-10-28):

- Analyze data models of the application landscape (strong database and SQL
  knowledge; designing data models)
- Define the data hierarchy for SDM and create the denorm data model
- Identify source tables for the SDM denorm and build extract / load
  workflows from the sources to the denorm
- Configure the SDM denorm

The profile is a **healthcare data engineer who also models data**. One senior
person covers it with strong SQL Server, data modeling, and payer data skills.

## By scope item

### 1. Analyze the landscape's data models

- **Expert SQL:**
  - reading an unfamiliar schema of hundreds of tables
  - finding keys and joins where no foreign keys are declared
  - profiling data: counts, null rates, value spreads
- **Payer domain knowledge:**
  - members, subscribers and dependents, groups, plans and products
  - claims and claim lines, providers and networks
  - FACETS' `CMC_*` tables saves weeks; on another platform (QNXT,
    HealthRules, Epic Tapestry) they learn that one
- **Working without documentation:** reverse-engineering from the data, then
  confirming with the customer's application team.

### 2. Define the hierarchy and the denorm model

- **Dimensional or denormalized modeling:** the grain of each table (one row
  per member, per claim, per claim line) and which attributes live where.
- **What SDM needs:**
  - the hierarchy: member → claims → lines, with the plan, benefit and
    provider context
  - the denorm tables (member, claims header and detail, plan and benefit,
    provider network) carry the features patterns are found on: age, gender,
    plan, provider specialty, network, in or out of network, PCP
- **Judgment on grain and size:** `plan_benefit_denorm` grew huge until it was
  cut to only what claim lines are billed under.

### 3. Source tables and the extract / load

- **Extract SQL that's safe on a large production system:** read-only,
  set-based, and kind to its indexes.
  - `sdg/packs/health/extract/FACETS.sql` is the template
  - a new source gets its own definitions file
- **Scheduling:** where create-table rights aren't granted, the loads run as
  the customer's scheduled jobs (Tidal at the first customer). Their
  scheduler and change process both matter.
- **Re-runnable loads:**
  - a complete refresh per table, stamped in `sdm.extract_log`
  - a failed table keeps its previous rows

### 4. Configure the SDM denorm

- **SQL Server basics:**
  - the `sdm` schema
  - logins and rights: db_ddladmin plus read and write, or owner of the schema
  - Windows authentication
  - certificate and Encrypt settings
- **Hands-on SDM:**
  - `.env` from `scripts/customer/customer.env.example`
  - `setup_tables`, `extract.cmd`, Settings > Pattern Settings (see
    `scripts/customer/README.md`)
  - no Python beyond running the scripts; comfortable with a command line and
    logs

## Not in the scope, but the PoC needs them

- **PHI and security discipline:**
  - production is read-only, and the pattern store is de-identified
  - the resource works inside the customer's access controls (often a VDI)
    and gets approval for production reads
- **Customer-facing skills:** running data-discovery sessions with the
  application and DBA teams, and walking them through the denorm design.
- **Optional, for the demo:** EDI (834, 837P, 835), to show generated files to
  the customer's testers.

## Staffing

- **One senior person** with SQL Server and payer data. The payer domain is
  the scarce part, so hire for it first.
- **Two people** if that mix isn't available:
  - a senior data modeler or analyst on the customer's core admin platform
    (items 1–2)
  - a SQL / ETL engineer (items 3–4)

**The biggest unknown is the customer's platform.** If it isn't FACETS, add
time to item 1: the extract definitions and the denorm mapping are written
from scratch.
