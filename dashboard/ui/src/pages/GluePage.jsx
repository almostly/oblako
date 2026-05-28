import { useState, useEffect } from 'react'
import Header from '@cloudscape-design/components/header'
import Table from '@cloudscape-design/components/table'
import Box from '@cloudscape-design/components/box'
import SpaceBetween from '@cloudscape-design/components/space-between'
import Button from '@cloudscape-design/components/button'
import Container from '@cloudscape-design/components/container'
import Tabs from '@cloudscape-design/components/tabs'
import Modal from '@cloudscape-design/components/modal'
import FormField from '@cloudscape-design/components/form-field'
import Input from '@cloudscape-design/components/input'
import Alert from '@cloudscape-design/components/alert'
import Badge from '@cloudscape-design/components/badge'
import StatusIndicator from '@cloudscape-design/components/status-indicator'
import ExpandableSection from '@cloudscape-design/components/expandable-section'
import CodeEditor from '../components/CodeEditor'

const API = 'http://localhost:8000'

const STARTER_SCRIPT = `# PySpark job — Glue 5 image with Iceberg + S3 already on the classpath.
# Spark reaches oblako's Iceberg REST catalog + S3Proxy via host.docker.internal.

from pyspark.sql import SparkSession

spark = (
    SparkSession.builder
    .appName("oblako-glue-demo")
    .config("spark.sql.catalog.iceberg", "org.apache.iceberg.spark.SparkCatalog")
    .config("spark.sql.catalog.iceberg.catalog-impl", "org.apache.iceberg.rest.RESTCatalog")
    .config("spark.sql.catalog.iceberg.uri", "http://host.docker.internal:8181")
    .config("spark.sql.catalog.iceberg.warehouse", "s3://oblako-iceberg/")
    .config("spark.sql.catalog.iceberg.s3.endpoint", "http://host.docker.internal:9000")
    .config("spark.sql.catalog.iceberg.s3.path-style-access", "true")
    .getOrCreate()
)

print("databases:", spark.sql("SHOW DATABASES IN iceberg").collect())
`

export default function GluePage() {
  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="AWS Glue. Data Catalog bridges to the Iceberg REST catalog (same tables Athena/Trino sees). Jobs run PySpark scripts in the official amazon/aws-glue-libs:5 image."
      >
        AWS Glue
      </Header>
      <Tabs tabs={[
        { id: 'catalog', label: 'Data Catalog', content: <CatalogTab /> },
        { id: 'jobs', label: 'Jobs', content: <JobsTab /> },
        { id: 'workflows', label: 'Workflows',
          content: (
            <Container>
              <Alert type="info" header="Workflows are not yet implemented">
                Glue Workflows orchestrate jobs + crawlers as a DAG. They're not in the
                oblako backend yet — Step Functions is the closest local alternative
                today. Track this in the project TODO.
              </Alert>
            </Container>
          ),
        },
      ]} />
    </SpaceBetween>
  )
}

function CatalogTab() {
  const [databases, setDatabases] = useState([])
  const [activeDb, setActiveDb] = useState(null)
  const [tables, setTables] = useState([])
  const [showCreate, setShowCreate] = useState(false)
  const [loading, setLoading] = useState(true)

  const refresh = () => {
    setLoading(true)
    fetch(`${API}/api/glue/databases`).then(r => r.json()).then(d => {
      setDatabases(d.databases || [])
      setLoading(false)
      if ((d.databases || []).length && !activeDb) {
        setActiveDb(d.databases[0].name)
      }
    })
  }
  // Effect body just kicks off async work — all setState happens inside .then().
  useEffect(() => {
    fetch(`${API}/api/glue/databases`).then(r => r.json()).then(d => {
      setDatabases(d.databases || [])
      setLoading(false)
      if (d.databases?.length) setActiveDb(d.databases[0].name)
    })
  }, [])

  useEffect(() => {
    if (!activeDb) return
    fetch(`${API}/api/glue/databases/${encodeURIComponent(activeDb)}/tables`)
      .then(r => r.json()).then(d => setTables(d.tables || []))
  }, [activeDb])

  return (
    <SpaceBetween size="l">
      <Container header={
        <Header variant="h2" counter={`(${databases.length})`}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button iconName="add-plus" onClick={() => setShowCreate(true)}>Create database</Button>
              <Button iconName="refresh" onClick={refresh}>Refresh</Button>
            </SpaceBetween>
          }>
          Databases
        </Header>
      }>
        <Table
          loading={loading}
          items={databases}
          selectionType="single"
          selectedItems={databases.filter(d => d.name === activeDb)}
          onSelectionChange={({ detail }) => setActiveDb(detail.selectedItems[0]?.name)}
          columnDefinitions={[
            { id: 'name', header: 'Name', cell: i => <Box variant="strong">{i.name}</Box> },
            { id: 'description', header: 'Description', cell: i => i.description || '—' },
          ]}
          empty={<Box textAlign="center">No databases yet.</Box>}
        />
      </Container>

      <Container header={
        <Header variant="h2" counter={`(${tables.length})`}
          description={activeDb ? `Tables in ${activeDb}` : 'Select a database above'}>
          Tables
        </Header>
      }>
        <Table
          items={tables}
          columnDefinitions={[
            { id: 'name', header: 'Name', cell: i => <Box variant="strong">{i.name}</Box> },
            { id: 'type', header: 'Type', cell: i => (
              <Badge color={i.parameters?.table_type === 'ICEBERG' ? 'blue' : 'grey'}>
                {i.parameters?.table_type || i.tableType || '—'}
              </Badge>
            )},
            { id: 'columns', header: 'Columns', cell: i => (
              <Box fontSize="body-s" variant="code">
                {(i.columns || []).map(c => `${c.name}:${c.type}`).join(', ') || '—'}
              </Box>
            )},
            { id: 'location', header: 'Location', cell: i => (
              <Box variant="code" fontSize="body-s">{i.location || '—'}</Box>
            )},
          ]}
          empty={
            <Box textAlign="center">
              {activeDb
                ? <>No tables in <b>{activeDb}</b> yet. Create one via pyiceberg or a Glue job.</>
                : 'Select a database.'}
            </Box>
          }
        />
      </Container>

      {showCreate && <CreateDbModal onClose={() => setShowCreate(false)} onCreated={refresh} />}
    </SpaceBetween>
  )
}

function CreateDbModal({ onClose, onCreated }) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [creating, setCreating] = useState(false)
  const [err, setErr] = useState(null)

  const create = async () => {
    setCreating(true); setErr(null)
    const r = await fetch(`${API}/api/glue/databases`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, description }),
    }).then(r => r.json())
    setCreating(false)
    if (r.error) { setErr(r.error); return }
    onCreated(); onClose()
  }

  return (
    <Modal visible onDismiss={onClose} header="Create Glue database" size="medium"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={onClose}>Cancel</Button>
            <Button variant="primary" loading={creating} disabled={!name} onClick={create}>Create</Button>
          </SpaceBetween>
        </Box>
      }>
      <SpaceBetween size="m">
        {err && <Alert type="error">{err}</Alert>}
        <FormField label="Name"><Input value={name} onChange={({ detail }) => setName(detail.value)} /></FormField>
        <FormField label="Description (optional)">
          <Input value={description} onChange={({ detail }) => setDescription(detail.value)} />
        </FormField>
      </SpaceBetween>
    </Modal>
  )
}

function JobsTab() {
  const [script, setScript] = useState(STARTER_SCRIPT)
  const [running, setRunning] = useState(false)
  const [latest, setLatest] = useState(null)
  const [history, setHistory] = useState([])

  // Effect body just kicks off async work — all setState happens inside .then().
  useEffect(() => {
    fetch(`${API}/api/glue/jobs/history`).then(r => r.json()).then(d => setHistory(d.jobs || []))
  }, [])

  const run = async () => {
    setRunning(true); setLatest(null)
    const r = await fetch(`${API}/api/glue/jobs/run`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ script }),
    }).then(r => r.json())
    setRunning(false); setLatest(r)
    fetch(`${API}/api/glue/jobs/history`).then(r => r.json()).then(d => setHistory(d.jobs || []))
  }

  return (
    <SpaceBetween size="l">
      <Container header={
        <Header variant="h2"
          actions={<Button variant="primary" iconName="play" loading={running} onClick={run}>Run job</Button>}
          description="PySpark in the official Glue 5 container. First run pulls ~5 GB and takes a few minutes.">
          PySpark script
        </Header>
      }>
        <CodeEditor value={script} onChange={setScript} language="python" rows={18} />
      </Container>

      {(latest || running) && (
        <Container header={
          <Header variant="h3"
            actions={latest && (
              <SpaceBetween direction="horizontal" size="xs">
                <StatusIndicator type={latest.exitCode === 0 ? 'success' : 'error'}>
                  {latest.exitCode === 0 ? 'SUCCESS' : `FAILED (exit ${latest.exitCode})`}
                </StatusIndicator>
                <Box variant="code" fontSize="body-s">{latest.durationMs} ms</Box>
              </SpaceBetween>
            )}>
            Latest run
          </Header>
        }>
          {running && <Box padding="m">Running… Spark startup alone is ~30 s; the page will update when the container exits.</Box>}
          {latest?.error && <Alert type="error">{latest.error}</Alert>}
          {latest?.logs && (
            <CodeEditor value={latest.logs} onChange={() => {}} language="javascript" rows={14} readOnly />
          )}
        </Container>
      )}

      <ExpandableSection headerText={`Run history (${history.length})`}>
        <Table
          items={history}
          columnDefinitions={[
            { id: 'name', header: 'Name', cell: i => <Box variant="code" fontSize="body-s">{i.name}</Box> },
            { id: 'status', header: 'Status', cell: i => (
              <StatusIndicator type={i.exitCode === 0 ? 'success' : 'error'}>
                {i.exitCode === 0 ? 'SUCCESS' : `exit ${i.exitCode}`}
              </StatusIndicator>
            )},
            { id: 'duration', header: 'Duration', cell: i => `${i.durationMs} ms` },
            { id: 'ranAt', header: 'Started', cell: i => <Box fontSize="body-s">{i.ranAt}</Box> },
          ]}
          empty={<Box textAlign="center">No runs yet.</Box>}
        />
      </ExpandableSection>
    </SpaceBetween>
  )
}
