import { useState, useEffect } from 'react'
import Header from '@cloudscape-design/components/header'
import Table from '@cloudscape-design/components/table'
import Box from '@cloudscape-design/components/box'
import SpaceBetween from '@cloudscape-design/components/space-between'
import Button from '@cloudscape-design/components/button'
import Container from '@cloudscape-design/components/container'
import Alert from '@cloudscape-design/components/alert'
import ExpandableSection from '@cloudscape-design/components/expandable-section'
import SegmentedControl from '@cloudscape-design/components/segmented-control'
import StatusIndicator from '@cloudscape-design/components/status-indicator'
import SqlEditor from '../components/SqlEditor'
import { useDuckDB, arrowToRows } from '../components/useDuckDB'

const API = 'http://localhost:8000'

// S3Proxy is always reached at the same hostname the dashboard is served from,
// port 9000. Building the URL dynamically keeps "localhost" out of the starter
// SQL whether the dashboard is opened at localhost, an alias from /etc/hosts,
// or a forwarded port.
const S3_BASE = `${window.location.protocol}//${window.location.hostname}:9000`

const STARTER_SQL = {
  trino: 'SELECT * FROM iceberg.credit.applicants LIMIT 50',
  duckdb: `-- DuckDB-Wasm reads parquet directly from S3Proxy over HTTP.
-- (CORS is on for local dev; no Trino, no Iceberg catalog — just parquet.)
-- Seed the demo file: uv run --extra iceberg python examples/athena-s3-tables/daily_sales.py
SELECT product_category, COUNT(*) AS units, SUM(sales_amount) AS revenue
FROM read_parquet('${S3_BASE}/oblako-iceberg/demos/daily_sales.parquet')
GROUP BY product_category
ORDER BY revenue DESC`,
}

export default function AthenaPage() {
  const [engine, setEngine] = useState('trino')
  const [sql, setSql] = useState(STARTER_SQL.trino)
  const [result, setResult] = useState(null)
  const [running, setRunning] = useState(false)
  const [schemas, setSchemas] = useState([])
  const { db: ddb, error: ddbError } = useDuckDB(engine === 'duckdb')

  useEffect(() => {
    fetch(`${API}/api/athena/schemas`).then(r => r.json()).then(d => setSchemas(d.schemas || []))
  }, [])

  const switchEngine = (id) => {
    setEngine(id); setResult(null)
    setSql(STARTER_SQL[id])
  }

  const runTrino = async () => {
    setRunning(true); setResult(null)
    try {
      const d = await fetch(`${API}/api/athena/query`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sql }),
      }).then(r => r.json())
      setResult(d)
    } catch (e) { setResult({ error: { message: String(e) } }) }
    setRunning(false)
  }

  const runDuckDB = async () => {
    if (!ddb) { setResult({ error: { message: 'DuckDB is still initialising (~10 MB Wasm bundle on first use).' } }); return }
    setRunning(true); setResult(null)
    let conn
    try {
      conn = await ddb.connect()
      const arrowTable = await conn.query(sql)
      setResult(arrowToRows(arrowTable))
    } catch (e) {
      setResult({ error: { message: String(e?.message || e) } })
    } finally {
      if (conn) await conn.close()
      setRunning(false)
    }
  }

  const runQuery = () => (engine === 'duckdb' ? runDuckDB() : runTrino())

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Athena-style SQL. Trino reads Iceberg/S3 Tables through the catalog; DuckDB-Wasm runs in your browser straight against parquet files on S3Proxy."
      >
        Athena
      </Header>

      <Container header={
        <Header variant="h2"
          actions={
            <SegmentedControl
              selectedId={engine}
              onChange={({ detail }) => switchEngine(detail.selectedId)}
              options={[
                { id: 'trino', text: 'Trino (catalog-aware)' },
                { id: 'duckdb', text: 'DuckDB-Wasm (in-browser)' },
              ]}
            />
          }>
          Query
        </Header>
      }>
        <SpaceBetween size="m">
          {engine === 'duckdb' && (
            <Box color="text-body-secondary" fontSize="body-s">
              Engine:{' '}
              {ddbError
                ? <StatusIndicator type="error">{ddbError}</StatusIndicator>
                : ddb
                  ? <StatusIndicator type="success">DuckDB ready (in-browser)</StatusIndicator>
                  : <StatusIndicator type="loading">Loading DuckDB-Wasm…</StatusIndicator>}
            </Box>
          )}
          <SqlEditor value={sql} onChange={setSql} rows={8} />
          <Button variant="primary" onClick={runQuery} loading={running}>Run</Button>

          {result?.error && (
            <Alert type="error">{result.error?.message || String(result.error)}</Alert>
          )}
          {result?.columns && (
            <Table
              header={<Header variant="h3" counter={`(${result.rows.length})`}>Results</Header>}
              items={result.rows.map((r, i) => ({ _idx: i, _row: r }))}
              columnDefinitions={result.columns.map((col, ci) => ({
                id: col, header: col,
                cell: item => <Box variant="code" fontSize="body-s">{String(item._row[ci] ?? '')}</Box>,
              }))}
              empty={<Box textAlign="center">No rows.</Box>}
            />
          )}
        </SpaceBetween>
      </Container>

      {engine === 'trino' && (
        <ExpandableSection headerText={`Catalog: iceberg (${schemas.length} schema${schemas.length === 1 ? '' : 's'})`}>
          <SpaceBetween size="s">
            {schemas.map(s => (
              <Box key={s.schema}>
                <Box variant="strong">{s.schema}</Box>
                {' '}
                {s.tables.map(t => (
                  <Button key={t} variant="link"
                    onClick={() => setSql(`SELECT * FROM iceberg.${s.schema}.${t} LIMIT 50`)}>
                    {t}
                  </Button>
                ))}
              </Box>
            ))}
            {!schemas.length && <Box color="text-status-inactive">No user schemas yet.</Box>}
          </SpaceBetween>
        </ExpandableSection>
      )}
    </SpaceBetween>
  )
}
