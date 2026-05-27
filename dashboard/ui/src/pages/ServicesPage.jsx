import { useState, useEffect } from 'react'
import Header from '@cloudscape-design/components/header'
import Table from '@cloudscape-design/components/table'
import Box from '@cloudscape-design/components/box'
import SpaceBetween from '@cloudscape-design/components/space-between'
import StatusIndicator from '@cloudscape-design/components/status-indicator'
import Container from '@cloudscape-design/components/container'
import ColumnLayout from '@cloudscape-design/components/column-layout'
import Button from '@cloudscape-design/components/button'

const API = 'http://localhost:8000'

export default function ServicesPage() {
  const [services, setServices] = useState([])
  const [loading, setLoading] = useState(true)

  const fetchServices = () => {
    setLoading(true)
    fetch(`${API}/api/services`)
      .then(r => r.json())
      .then(data => { setServices(data.services || []); setLoading(false) })
      .catch(() => setLoading(false))
  }

  useEffect(() => { fetchServices() }, [])

  const running = services.filter(s => s.status === 'running').length
  const total = services.length

  return (
    <SpaceBetween size="l">
      <Header variant="h1" actions={<Button onClick={fetchServices} iconName="refresh">Refresh</Button>}>
        oblako Console
      </Header>

      <ColumnLayout columns={3}>
        <Container>
          <Box variant="awsui-key-label">Services running</Box>
          <Box variant="awsui-value-large">{running} / {total}</Box>
        </Container>
        <Container>
          <Box variant="awsui-key-label">Region</Box>
          <Box variant="awsui-value-large">local</Box>
        </Container>
        <Container>
          <Box variant="awsui-key-label">Version</Box>
          <Box variant="awsui-value-large">0.1.0</Box>
        </Container>
      </ColumnLayout>

      <Table
        header={<Header variant="h2">Services</Header>}
        loading={loading}
        items={services}
        columnDefinitions={[
          {
            id: 'name',
            header: 'Service',
            cell: item => <Box fontWeight="bold">{item.name}</Box>,
            sortingField: 'name',
          },
          {
            id: 'type',
            header: 'AWS Equivalent',
            cell: item => item.type,
          },
          {
            id: 'status',
            header: 'Status',
            cell: item => (
              <StatusIndicator type={item.status === 'running' ? 'success' : item.status === 'idle' ? 'info' : 'stopped'}>
                {item.status}
              </StatusIndicator>
            ),
          },
        ]}
        empty={<Box textAlign="center">No services found. Run: oblako up</Box>}
      />
    </SpaceBetween>
  )
}
