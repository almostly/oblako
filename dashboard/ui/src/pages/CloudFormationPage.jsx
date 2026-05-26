import { useState, useEffect } from 'react'
import Header from '@cloudscape-design/components/header'
import Table from '@cloudscape-design/components/table'
import Box from '@cloudscape-design/components/box'
import SpaceBetween from '@cloudscape-design/components/space-between'
import StatusIndicator from '@cloudscape-design/components/status-indicator'
import Button from '@cloudscape-design/components/button'
import Container from '@cloudscape-design/components/container'
import ColumnLayout from '@cloudscape-design/components/column-layout'
import Tabs from '@cloudscape-design/components/tabs'

const API = 'http://localhost:8000'

function statusType(s) {
  if (!s) return 'pending'
  if (s.endsWith('FAILED')) return 'error'
  if (s.includes('IN_PROGRESS') || s.includes('REVIEW')) return 'in-progress'
  if (s.includes('DELETE')) return 'stopped'
  return 'success'
}

export default function CloudFormationPage() {
  const [stacks, setStacks] = useState([])
  const [selected, setSelected] = useState(null)
  const [detail, setDetail] = useState(null)
  const [loading, setLoading] = useState(true)

  const fetchStacks = () => {
    setLoading(true)
    fetch(`${API}/api/cloudformation/stacks`)
      .then(r => r.json())
      .then(data => { setStacks(data.stacks || []); setLoading(false) })
      .catch(() => setLoading(false))
  }

  useEffect(() => { fetchStacks() }, [])

  const openStack = (name) => {
    setSelected(name)
    setDetail(null)
    fetch(`${API}/api/cloudformation/stacks/${encodeURIComponent(name)}`)
      .then(r => r.json())
      .then(setDetail)
  }

  if (selected && detail && !detail.error) {
    return (
      <SpaceBetween size="l">
        <Header variant="h1" actions={<Button onClick={() => { setSelected(null); setDetail(null) }}>Back</Button>}>
          {detail.stackName}
        </Header>

        <ColumnLayout columns={3}>
          <Container>
            <Box variant="awsui-key-label">Status</Box>
            <StatusIndicator type={statusType(detail.stackStatus)}>{detail.stackStatus}</StatusIndicator>
          </Container>
          <Container>
            <Box variant="awsui-key-label">Created</Box>
            <Box variant="awsui-value-large">{detail.creationTime}</Box>
          </Container>
          <Container>
            <Box variant="awsui-key-label">Resources</Box>
            <Box variant="awsui-value-large">{(detail.resources || []).length}</Box>
          </Container>
        </ColumnLayout>

        <Tabs tabs={[
          {
            id: 'resources',
            label: `Resources (${(detail.resources || []).length})`,
            content: (
              <Table
                header={<Header variant="h2">Resources</Header>}
                items={detail.resources || []}
                columnDefinitions={[
                  { id: 'logicalId', header: 'Logical ID', cell: item => <Box fontWeight="bold">{item.logicalId}</Box> },
                  { id: 'type', header: 'Type', cell: item => <Box variant="code" fontSize="body-s">{item.type}</Box> },
                  { id: 'physicalId', header: 'Physical ID', cell: item => item.physicalId },
                  { id: 'status', header: 'Status', cell: item => <StatusIndicator type={statusType(item.status)}>{item.status}</StatusIndicator> },
                ]}
                empty={<Box textAlign="center">No resources.</Box>}
              />
            ),
          },
          {
            id: 'outputs',
            label: `Outputs (${(detail.outputs || []).length})`,
            content: (
              <Table
                header={<Header variant="h2">Outputs</Header>}
                items={detail.outputs || []}
                columnDefinitions={[
                  { id: 'key', header: 'Key', cell: item => <Box fontWeight="bold">{item.key}</Box> },
                  { id: 'value', header: 'Value', cell: item => <Box variant="code" fontSize="body-s">{item.value}</Box> },
                ]}
                empty={<Box textAlign="center">No outputs.</Box>}
              />
            ),
          },
          {
            id: 'events',
            label: `Events (${(detail.events || []).length})`,
            content: (
              <Table
                header={<Header variant="h2">Events</Header>}
                items={detail.events || []}
                columnDefinitions={[
                  { id: 'timestamp', header: 'Timestamp', cell: item => item.timestamp },
                  { id: 'logicalId', header: 'Logical ID', cell: item => item.logicalId },
                  { id: 'type', header: 'Type', cell: item => <Box variant="code" fontSize="body-s">{item.type}</Box> },
                  { id: 'status', header: 'Status', cell: item => <StatusIndicator type={statusType(item.status)}>{item.status}</StatusIndicator> },
                  { id: 'reason', header: 'Reason', cell: item => item.reason || '-' },
                ]}
                empty={<Box textAlign="center">No events.</Box>}
              />
            ),
          },
        ]} />
      </SpaceBetween>
    )
  }

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        actions={<Button onClick={fetchStacks} iconName="refresh">Refresh</Button>}
        description="Stacks deployed to oblako's local CloudFormation (:5601) — resources are provisioned into the real engines."
      >
        AWS CloudFormation
      </Header>
      <Table
        header={<Header variant="h2">Stacks</Header>}
        loading={loading}
        items={stacks}
        columnDefinitions={[
          { id: 'name', header: 'Stack', cell: item => (
            <Button variant="link" onClick={() => openStack(item.stackName)}>{item.stackName}</Button>
          )},
          { id: 'status', header: 'Status', cell: item => <StatusIndicator type={statusType(item.stackStatus)}>{item.stackStatus}</StatusIndicator> },
          { id: 'outputs', header: 'Outputs', cell: item => item.outputCount },
          { id: 'created', header: 'Created', cell: item => item.creationTime },
        ]}
        empty={<Box textAlign="center">No stacks. Deploy one: <Box variant="code">aws cloudformation deploy --endpoint-url http://localhost:5601</Box></Box>}
      />
    </SpaceBetween>
  )
}
