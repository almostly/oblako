import { useState, useEffect, useRef } from 'react'
import Header from '@cloudscape-design/components/header'
import Table from '@cloudscape-design/components/table'
import Box from '@cloudscape-design/components/box'
import SpaceBetween from '@cloudscape-design/components/space-between'
import Container from '@cloudscape-design/components/container'
import Button from '@cloudscape-design/components/button'
import Modal from '@cloudscape-design/components/modal'
import FormField from '@cloudscape-design/components/form-field'
import Input from '@cloudscape-design/components/input'
import Alert from '@cloudscape-design/components/alert'
import Prism from 'prismjs'
import 'prismjs/components/prism-sql'
import 'prismjs/themes/prism.css'

const API = 'http://localhost:8000'

function SqlEditor({ value, onChange, rows = 5 }) {
  const highlightRef = useRef(null)
  useEffect(() => {
    if (highlightRef.current) Prism.highlightElement(highlightRef.current)
  }, [value])
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
        <code ref={highlightRef} className="language-sql">{value + '\n'}</code>
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

export default function RedshiftPage() {
  const [clusters, setClusters] = useState([])
  const [tables, setTables] = useState([])
  const [query, setQuery] = useState('SELECT table_name FROM information_schema.tables WHERE table_schema = \'public\'')
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(false)
  const [show, setShow] = useState(false)
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState(null)
  const [cform, setCform] = useState({
    clusterIdentifier: '', nodeType: 'ra3.xlplus', numberOfNodes: '1',
    dbName: 'dev', masterUsername: 'admin', masterUserPassword: 'Password123',
  })
  const setC = (k, v) => setCform(f => ({ ...f, [k]: v }))

  const fetchClusters = () => {
    fetch(`${API}/api/redshift/clusters`).then(r => r.json()).then(d => setClusters(d.clusters || []))
  }

  useEffect(() => {
    fetchClusters()
    fetch(`${API}/api/redshift/tables`).then(r => r.json()).then(data => setTables(data.tables || []))
  }, [])

  const createCluster = () => {
    if (!cform.clusterIdentifier) { setError('Cluster identifier is required'); return }
    setCreating(true); setError(null)
    fetch(`${API}/api/redshift/clusters`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cform),
    })
      .then(r => r.json())
      .then(d => { setCreating(false); if (d.error) { setError(d.error); return } setShow(false); fetchClusters() })
      .catch(e => { setCreating(false); setError(String(e)) })
  }

  const runQuery = () => {
    setLoading(true)
    fetch(`${API}/api/redshift/query`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query }),
    })
      .then(r => r.json())
      .then(data => { setResult(data); setLoading(false) })
      .catch(e => { setResult({ error: e.message }); setLoading(false) })
  }

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        actions={<Button iconName="add-plus" onClick={() => { setError(null); setShow(true) }}>Create cluster</Button>}
      >
        Amazon Redshift
      </Header>

      <Table
        header={<Header variant="h2" counter={`(${clusters.length})`}>Clusters</Header>}
        items={clusters}
        columnDefinitions={[
          { id: 'id', header: 'Cluster', cell: c => <Box fontWeight="bold">{c.ClusterIdentifier}</Box> },
          { id: 'status', header: 'Status', cell: c => c.ClusterStatus },
          { id: 'nodeType', header: 'Node type', cell: c => c.NodeType },
          { id: 'nodes', header: 'Nodes', cell: c => c.NumberOfNodes },
          { id: 'endpoint', header: 'Endpoint', cell: c => c.Endpoint?.Address ? `${c.Endpoint.Address}:${c.Endpoint.Port}` : '—' },
        ]}
        empty={<Box textAlign="center">No clusters. Create one with the redshift client.</Box>}
      />

      <Table
        header={<Header variant="h2">Tables</Header>}
        items={tables.map(t => ({ name: t }))}
        columnDefinitions={[
          { id: 'name', header: 'Table name', cell: item => <Box fontWeight="bold">{item.name}</Box> },
          {
            id: 'action', header: '', cell: item => (
              <Button variant="link" onClick={() => { setQuery(`SELECT * FROM ${item.name} LIMIT 50`); }}>
                Query
              </Button>
            )
          },
        ]}
        empty={<Box textAlign="center">No tables found.</Box>}
      />

      <Container header={<Header variant="h2">Query editor</Header>}>
        <SpaceBetween size="m">
          <SqlEditor value={query} onChange={setQuery} rows={5} />
          <Button variant="primary" onClick={runQuery} loading={loading}>Run query</Button>

          {result?.error && <Box color="text-status-error">{result.error}</Box>}
          {result?.message && <Box color="text-status-success">{result.message}</Box>}
          {result?.columns && (
            <Table
              items={result.rows}
              columnDefinitions={result.columns.map(col => ({
                id: col,
                header: col,
                cell: item => String(item[col] ?? ''),
              }))}
              empty={<Box textAlign="center">No rows returned.</Box>}
            />
          )}
        </SpaceBetween>
      </Container>

      <Modal
        visible={show}
        onDismiss={() => setShow(false)}
        header="Create Redshift cluster"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShow(false)}>Cancel</Button>
              <Button variant="primary" loading={creating} onClick={createCluster}>Create</Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {error && <Alert type="error">{error}</Alert>}
          <FormField label="Cluster identifier">
            <Input value={cform.clusterIdentifier} onChange={({ detail }) => setC('clusterIdentifier', detail.value)} placeholder="analytics-cluster" />
          </FormField>
          <FormField label="Node type">
            <Input value={cform.nodeType} onChange={({ detail }) => setC('nodeType', detail.value)} />
          </FormField>
          <FormField label="Number of nodes">
            <Input type="number" value={cform.numberOfNodes} onChange={({ detail }) => setC('numberOfNodes', detail.value)} />
          </FormField>
          <FormField label="Database name">
            <Input value={cform.dbName} onChange={({ detail }) => setC('dbName', detail.value)} />
          </FormField>
          <FormField label="Master username">
            <Input value={cform.masterUsername} onChange={({ detail }) => setC('masterUsername', detail.value)} />
          </FormField>
          <FormField label="Master password">
            <Input type="password" value={cform.masterUserPassword} onChange={({ detail }) => setC('masterUserPassword', detail.value)} />
          </FormField>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  )
}
