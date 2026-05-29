import { useState, useEffect } from 'react'
import Header from '@cloudscape-design/components/header'
import Table from '@cloudscape-design/components/table'
import Box from '@cloudscape-design/components/box'
import SpaceBetween from '@cloudscape-design/components/space-between'
import Button from '@cloudscape-design/components/button'
import StatusIndicator from '@cloudscape-design/components/status-indicator'
import Badge from '@cloudscape-design/components/badge'
import Modal from '@cloudscape-design/components/modal'
import FormField from '@cloudscape-design/components/form-field'
import Input from '@cloudscape-design/components/input'
import Select from '@cloudscape-design/components/select'
import SegmentedControl from '@cloudscape-design/components/segmented-control'
import Alert from '@cloudscape-design/components/alert'

const API = 'http://localhost:8000'
const ENGINES = [
  { value: 'postgres', label: 'PostgreSQL' },
  { value: 'mysql', label: 'MySQL' },
]

export default function RdsPage() {
  const [databases, setDatabases] = useState([])
  const [loading, setLoading] = useState(true)
  const [show, setShow] = useState(false)
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState(null)
  const [form, setForm] = useState({
    mode: 'instance', engine: 'postgres', identifier: '', dbName: 'app',
    masterUsername: 'admin', masterUserPassword: 'Password123', instanceClass: 'db.t3.micro',
  })
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const fetchDatabases = () => {
    setLoading(true)
    fetch(`${API}/api/rds/databases`).then(r => r.json())
      .then(d => { setDatabases(d.databases || []); setLoading(false) })
      .catch(() => setLoading(false))
  }
  useEffect(fetchDatabases, [])

  const create = () => {
    if (!form.identifier) { setError('Identifier is required'); return }
    setCreating(true); setError(null)
    fetch(`${API}/api/rds/databases`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(form),
    })
      .then(r => r.json())
      .then(d => {
        setCreating(false)
        if (d.error) { setError(d.error); return }
        setShow(false); fetchDatabases()
      })
      .catch(e => { setCreating(false); setError(String(e)) })
  }

  const engineOpt = ENGINES.find(e => e.value === form.engine)

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button iconName="add-plus" onClick={() => { setError(null); setShow(true) }}>Create database</Button>
            <Button iconName="refresh" onClick={fetchDatabases}>Refresh</Button>
          </SpaceBetween>
        }
        description="RDS instances and Aurora clusters. Control plane via moto; real SQL runs on the local engine."
      >
        RDS / Aurora
      </Header>

      <Table
        header={<Header variant="h2" counter={`(${databases.length})`}>Databases</Header>}
        loading={loading}
        items={databases}
        columnDefinitions={[
          { id: 'id', header: 'Identifier', cell: i => <Box fontWeight="bold">{i.id}</Box> },
          { id: 'kind', header: 'Type', cell: i => <Badge color={i.kind === 'cluster' ? 'blue' : 'grey'}>{i.kind === 'cluster' ? 'Aurora cluster' : 'instance'}</Badge> },
          { id: 'engine', header: 'Engine', cell: i => i.engine },
          { id: 'class', header: 'Class', cell: i => i.instanceClass || '—' },
          { id: 'status', header: 'Status', cell: i => <StatusIndicator type={i.status === 'available' ? 'success' : 'in-progress'}>{i.status}</StatusIndicator> },
          { id: 'endpoint', header: 'Endpoint', cell: i => <Box variant="code" fontSize="body-s">{i.endpoint || '—'}</Box> },
        ]}
        empty={<Box textAlign="center">No databases yet. Create one.</Box>}
      />

      <Modal
        visible={show}
        onDismiss={() => setShow(false)}
        header="Create database"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShow(false)}>Cancel</Button>
              <Button variant="primary" loading={creating} onClick={create}>Create</Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {error && <Alert type="error">{error}</Alert>}
          <FormField label="Type">
            <SegmentedControl
              selectedId={form.mode}
              onChange={({ detail }) => set('mode', detail.selectedId)}
              options={[
                { id: 'instance', text: 'DB instance' },
                { id: 'cluster', text: 'Aurora cluster' },
              ]}
            />
          </FormField>
          <FormField label="Engine">
            <Select
              selectedOption={engineOpt}
              onChange={({ detail }) => set('engine', detail.selectedOption.value)}
              options={ENGINES}
            />
          </FormField>
          <FormField label="Identifier">
            <Input value={form.identifier} onChange={({ detail }) => set('identifier', detail.value)} placeholder="credit-db" />
          </FormField>
          <FormField label="Database name">
            <Input value={form.dbName} onChange={({ detail }) => set('dbName', detail.value)} />
          </FormField>
          {form.mode === 'instance' && (
            <FormField label="Instance class">
              <Input value={form.instanceClass} onChange={({ detail }) => set('instanceClass', detail.value)} />
            </FormField>
          )}
          <FormField label="Master username">
            <Input value={form.masterUsername} onChange={({ detail }) => set('masterUsername', detail.value)} />
          </FormField>
          <FormField label="Master password">
            <Input type="password" value={form.masterUserPassword} onChange={({ detail }) => set('masterUserPassword', detail.value)} />
          </FormField>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  )
}
