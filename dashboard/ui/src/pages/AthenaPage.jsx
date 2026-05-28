import { useState, useEffect } from 'react'
import Header from '@cloudscape-design/components/header'
import Table from '@cloudscape-design/components/table'
import Box from '@cloudscape-design/components/box'
import SpaceBetween from '@cloudscape-design/components/space-between'
import Button from '@cloudscape-design/components/button'
import Container from '@cloudscape-design/components/container'
import Alert from '@cloudscape-design/components/alert'
import ExpandableSection from '@cloudscape-design/components/expandable-section'
import SqlEditor from '../components/SqlEditor'

const API = 'http://localhost:8000'

export default function AthenaPage() {
  const [sql, setSql] = useState('SELECT * FROM iceberg.credit.applicants LIMIT 50')
  const [result, setResult] = useState(null)
  const [running, setRunning] = useState(false)
  const [schemas, setSchemas] = useState([])

  useEffect(() => {
    fetch(`${API}/api/athena/schemas`).then(r => r.json()).then(d => setSchemas(d.schemas || []))
  }, [])

  const runQuery = () => {
    setRunning(true); setResult(null)
    fetch(`${API}/api/athena/query`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sql }),
    }).then(r => r.json()).then(d => { setResult(d); setRunning(false) })
      .catch(e => { setResult({ error: { message: String(e) } }); setRunning(false) })
  }

  return (
    <SpaceBetween size="l">
      <Header variant="h1"
        description="Athena-style SQL via Trino. Tables come from the Iceberg / S3 Tables catalog.">
        Athena
      </Header>

      <Container header={<Header variant="h2">Query</Header>}>
        <SpaceBetween size="m">
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
    </SpaceBetween>
  )
}
