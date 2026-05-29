import { useState, useEffect } from 'react'
import Header from '@cloudscape-design/components/header'
import Table from '@cloudscape-design/components/table'
import Box from '@cloudscape-design/components/box'
import SpaceBetween from '@cloudscape-design/components/space-between'
import Button from '@cloudscape-design/components/button'
import StatusIndicator from '@cloudscape-design/components/status-indicator'
import Modal from '@cloudscape-design/components/modal'
import FormField from '@cloudscape-design/components/form-field'
import Input from '@cloudscape-design/components/input'
import Container from '@cloudscape-design/components/container'
import Alert from '@cloudscape-design/components/alert'

const API = 'http://localhost:8000'

export default function KinesisPage() {
  const [streams, setStreams] = useState([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState(null)
  const [records, setRecords] = useState([])
  const [show, setShow] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const [form, setForm] = useState({ streamName: '', shardCount: '1' })
  const [putForm, setPutForm] = useState({ partitionKey: 'p1', data: '' })

  const fetchStreams = () => {
    setLoading(true)
    fetch(`${API}/api/kinesis/streams`).then(r => r.json())
      .then(d => { setStreams(d.streams || []); setLoading(false) })
      .catch(() => setLoading(false))
  }
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(fetchStreams, [])

  const create = () => {
    if (!form.streamName) { setErr('Stream name is required'); return }
    setBusy(true); setErr(null)
    fetch(`${API}/api/kinesis/streams`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(form),
    }).then(r => r.json()).then(d => {
      setBusy(false)
      if (d.error) { setErr(d.error); return }
      setShow(false); fetchStreams()
    })
  }

  const fetchRecords = (name) => {
    setSelected(name)
    fetch(`${API}/api/kinesis/records/${encodeURIComponent(name)}`).then(r => r.json())
      .then(d => setRecords(d.records || []))
  }

  const putRecord = () => {
    fetch(`${API}/api/kinesis/records`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ streamName: selected, ...putForm }),
    }).then(() => fetchRecords(selected))
  }

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Local Kinesis Data Streams via kinesalite. boto3 'kinesis' clients work unchanged."
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button iconName="add-plus" onClick={() => { setErr(null); setShow(true) }}>Create stream</Button>
            <Button iconName="refresh" onClick={fetchStreams}>Refresh</Button>
          </SpaceBetween>
        }
      >
        Amazon Kinesis
      </Header>

      <Table
        header={<Header variant="h2" counter={`(${streams.length})`}>Streams</Header>}
        loading={loading}
        items={streams}
        columnDefinitions={[
          { id: 'name', header: 'Name', cell: i => (
            <Button variant="link" onClick={() => fetchRecords(i.name)}>{i.name}</Button>
          )},
          { id: 'status', header: 'Status', cell: i => (
            <StatusIndicator type={i.status === 'ACTIVE' ? 'success' : 'in-progress'}>{i.status}</StatusIndicator>
          )},
          { id: 'shards', header: 'Shards', cell: i => i.shards },
        ]}
        empty={<Box textAlign="center">No streams yet. Create one.</Box>}
      />

      {selected && (
        <Container header={<Header variant="h2" actions={
          <Button onClick={() => fetchRecords(selected)} iconName="refresh">Refresh</Button>
        }>{selected}</Header>}>
          <SpaceBetween size="m">
            <FormField label="Partition key">
              <Input value={putForm.partitionKey} onChange={({ detail }) => setPutForm(f => ({ ...f, partitionKey: detail.value }))} />
            </FormField>
            <FormField label="Data (string)">
              <Input value={putForm.data} onChange={({ detail }) => setPutForm(f => ({ ...f, data: detail.value }))} placeholder="hello kinesis" />
            </FormField>
            <Button variant="primary" onClick={putRecord} disabled={!putForm.data}>Put record</Button>
            <Table
              header={<Header variant="h3" counter={`(${records.length})`}>Records (TRIM_HORIZON)</Header>}
              items={records}
              columnDefinitions={[
                { id: 'pk', header: 'Partition key', cell: r => r.partitionKey },
                { id: 'data', header: 'Data', cell: r => <Box variant="code" fontSize="body-s">{r.data}</Box> },
                { id: 'seq', header: 'Sequence', cell: r => <Box variant="code" fontSize="body-s">{r.sequenceNumber.slice(0, 16)}…</Box> },
              ]}
              empty={<Box textAlign="center">No records.</Box>}
            />
          </SpaceBetween>
        </Container>
      )}

      <Modal
        visible={show}
        onDismiss={() => setShow(false)}
        header="Create Kinesis stream"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShow(false)}>Cancel</Button>
              <Button variant="primary" loading={busy} onClick={create}>Create</Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {err && <Alert type="error">{err}</Alert>}
          <FormField label="Stream name">
            <Input value={form.streamName} onChange={({ detail }) => setForm(f => ({ ...f, streamName: detail.value }))} placeholder="events" />
          </FormField>
          <FormField label="Shard count">
            <Input type="number" value={form.shardCount} onChange={({ detail }) => setForm(f => ({ ...f, shardCount: detail.value }))} />
          </FormField>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  )
}
