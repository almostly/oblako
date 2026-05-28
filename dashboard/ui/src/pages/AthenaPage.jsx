import { useState, useEffect, useRef } from 'react'
import Header from '@cloudscape-design/components/header'
import Table from '@cloudscape-design/components/table'
import Box from '@cloudscape-design/components/box'
import SpaceBetween from '@cloudscape-design/components/space-between'
import Button from '@cloudscape-design/components/button'
import Container from '@cloudscape-design/components/container'
import Alert from '@cloudscape-design/components/alert'
import ExpandableSection from '@cloudscape-design/components/expandable-section'
import Prism from 'prismjs'
import 'prismjs/components/prism-sql'
import 'prismjs/themes/prism.css'

const API = 'http://localhost:8000'

function SqlEditor({ value, onChange, rows = 8 }) {
  const ref = useRef(null)
  useEffect(() => { if (ref.current) Prism.highlightElement(ref.current) }, [value])
  const minHeight = rows * 22
  return (
    <div style={{ position: 'relative', minHeight, borderRadius: 4, border: '1px solid #d5dbdb', overflow: 'hidden' }}>
      <pre aria-hidden="true" style={{
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
        fontSize: 13, lineHeight: '1.5', padding: 10, margin: 0,
        position: 'absolute', top: 0, left: 0, right: 0, bottom: 0,
        background: 'transparent', pointerEvents: 'none', zIndex: 1,
        whiteSpace: 'pre-wrap', wordWrap: 'break-word', overflow: 'auto',
      }}>
        <code ref={ref} className="language-sql">{value + '\n'}</code>
      </pre>
      <textarea
        value={value}
        onChange={e => onChange(e.target.value)}
        spellCheck={false}
        style={{
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
          fontSize: 13, lineHeight: '1.5', padding: 10, margin: 0,
          position: 'relative', zIndex: 2,
          width: '100%', minHeight, resize: 'vertical',
          background: 'transparent', color: 'transparent',
          caretColor: '#000', border: 'none', outline: 'none',
          whiteSpace: 'pre-wrap', wordWrap: 'break-word',
        }}
      />
    </div>
  )
}

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
