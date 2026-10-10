import { useState, useEffect } from 'react'
import Header from '@cloudscape-design/components/header'
import Table from '@cloudscape-design/components/table'
import Box from '@cloudscape-design/components/box'
import SpaceBetween from '@cloudscape-design/components/space-between'
import StatusIndicator from '@cloudscape-design/components/status-indicator'
import Container from '@cloudscape-design/components/container'
import Button from '@cloudscape-design/components/button'

const API = 'http://localhost:8000'

export default function ServicesPage() {
  const [services, setServices] = useState([])
  const [loading, setLoading] = useState(true)
  const [cfg, setCfg] = useState({ region: '—', accountId: '—' })

  const fetchServices = () => {
    setLoading(true)
    fetch(`${API}/api/services`)
      .then(r => r.json())
      .then(data => { setServices(data.services || []); setLoading(false) })
      .catch(() => setLoading(false))
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    fetchServices()
    fetch(`${API}/api/config`).then(r => r.json()).then(setCfg).catch(() => {})
  }, [])

  const running = services.filter(s => s.status === 'running').length
  const total = services.length

  return (
    <SpaceBetween size="l">
      <Header variant="h1" actions={<Button onClick={fetchServices} iconName="refresh">Refresh</Button>}>
        oblako Console
      </Header>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 20, alignItems: 'stretch' }}>
        <Container fitHeight>
          <Box variant="awsui-key-label">Services running</Box>
          <Box variant="awsui-value-large">{running} / {total}</Box>
        </Container>
        <Container fitHeight>
          <Box variant="awsui-key-label">Region / account</Box>
          <Box variant="awsui-value-large">{cfg.region}</Box>
          <Box color="text-status-inactive" fontSize="body-s">{cfg.accountId}</Box>
        </Container>
        <Container fitHeight>
          <Box variant="awsui-key-label">Version</Box>
          <Box variant="awsui-value-large">0.3.0</Box>
        </Container>
      </div>

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
