import { useState, useEffect } from 'react'
import Header from '@cloudscape-design/components/header'
import Table from '@cloudscape-design/components/table'
import Box from '@cloudscape-design/components/box'
import SpaceBetween from '@cloudscape-design/components/space-between'
import Button from '@cloudscape-design/components/button'
import Container from '@cloudscape-design/components/container'
import Modal from '@cloudscape-design/components/modal'
import FormField from '@cloudscape-design/components/form-field'
import Input from '@cloudscape-design/components/input'
import Select from '@cloudscape-design/components/select'
import Alert from '@cloudscape-design/components/alert'
import Badge from '@cloudscape-design/components/badge'
import StatusIndicator from '@cloudscape-design/components/status-indicator'

const API = 'http://localhost:8000'

const TYPES = ['t3.micro', 't3.small', 't3.medium', 't3.large', 'm5.large', 'c5.xlarge']
  .map(v => ({ value: v, label: v }))

const stateType = (s) =>
  s === 'running' ? 'success' : s === 'stopped' ? 'stopped'
  : s === 'pending' || s === 'stopping' ? 'in-progress' : 'pending'

export default function Ec2Page() {
  const [instances, setInstances] = useState([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState([])
  const [showLaunch, setShowLaunch] = useState(false)
  const [busy, setBusy] = useState(false)

  const refresh = () => {
    fetch(`${API}/api/ec2/instances`).then(r => r.json()).then(d => {
      setInstances(d.instances || [])
      setLoading(false)
    })
  }
  // Effect body just kicks off async work — all setState happens inside .then().
  useEffect(() => {
    fetch(`${API}/api/ec2/instances`).then(r => r.json()).then(d => {
      setInstances(d.instances || [])
      setLoading(false)
    })
  }, [])

  const act = async (path, method = 'POST') => {
    setBusy(true)
    await Promise.all(selected.map(i =>
      fetch(`${API}/api/ec2/instances/${i.id}${path}`, { method }).then(r => r.json())))
    setBusy(false); setSelected([]); refresh()
  }

  return (
    <SpaceBetween size="l">
      <Header variant="h1"
        description="AWS EC2. Control plane via moto; each instance is backed by a real Docker container with a Docker named volume as its EBS root (instance == container, EBS == volume).">
        EC2
      </Header>
      <Container header={
        <Header variant="h2" counter={`(${instances.length})`}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button disabled={!selected.length || busy} loading={busy}
                onClick={() => act('/start')}>Start</Button>
              <Button disabled={!selected.length || busy} loading={busy}
                onClick={() => act('/stop')}>Stop</Button>
              <Button disabled={!selected.length || busy} loading={busy}
                onClick={() => act('', 'DELETE')}>Terminate</Button>
              <Button iconName="refresh" onClick={refresh}>Refresh</Button>
              <Button variant="primary" iconName="add-plus" onClick={() => setShowLaunch(true)}>
                Launch instance
              </Button>
            </SpaceBetween>
          }>
          Instances
        </Header>
      }>
        <Table
          loading={loading}
          items={instances}
          selectionType="multi"
          selectedItems={selected}
          onSelectionChange={({ detail }) => setSelected(detail.selectedItems)}
          trackBy="id"
          columnDefinitions={[
            { id: 'id', header: 'Instance ID', cell: i => <Box variant="code" fontSize="body-s">{i.id}</Box> },
            { id: 'name', header: 'Name', cell: i => i.name || '—' },
            { id: 'type', header: 'Type', cell: i => <Badge>{i.type}</Badge> },
            { id: 'state', header: 'State', cell: i => (
              <StatusIndicator type={stateType(i.state)}>{i.state}</StatusIndicator>
            )},
            { id: 'container', header: 'Container', cell: i => (
              <Box fontSize="body-s" color="text-status-inactive">{i.containerStatus}</Box>
            )},
            { id: 'image', header: 'AMI', cell: i => <Box fontSize="body-s">{i.imageId}</Box> },
          ]}
          empty={<Box textAlign="center">No instances. Launch one — it starts a real container.</Box>}
        />
      </Container>
      {showLaunch && <LaunchModal onClose={() => setShowLaunch(false)} onLaunched={refresh} />}
    </SpaceBetween>
  )
}

function LaunchModal({ onClose, onLaunched }) {
  const [name, setName] = useState('')
  const [type, setType] = useState(TYPES[0])
  const [launching, setLaunching] = useState(false)
  const [err, setErr] = useState(null)

  const launch = async () => {
    setLaunching(true); setErr(null)
    const r = await fetch(`${API}/api/ec2/instances`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ instanceType: type.value, name }),
    }).then(r => r.json())
    setLaunching(false)
    if (r.error) { setErr(r.error); return }
    onLaunched(); onClose()
  }

  return (
    <Modal visible onDismiss={onClose} header="Launch instance" size="medium"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={onClose}>Cancel</Button>
            <Button variant="primary" loading={launching} onClick={launch}>Launch</Button>
          </SpaceBetween>
        </Box>
      }>
      <SpaceBetween size="m">
        {err && <Alert type="error">{err}</Alert>}
        <Alert type="info">Launching starts a real Docker container (amazonlinux:2023) with an EBS-backed volume. First launch pulls the image.</Alert>
        <FormField label="Name (optional)">
          <Input value={name} onChange={({ detail }) => setName(detail.value)} placeholder="my-instance" />
        </FormField>
        <FormField label="Instance type">
          <Select selectedOption={type} onChange={({ detail }) => setType(detail.selectedOption)} options={TYPES} />
        </FormField>
      </SpaceBetween>
    </Modal>
  )
}
