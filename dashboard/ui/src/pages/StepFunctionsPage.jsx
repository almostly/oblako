import { useState, useEffect, useRef } from 'react'
import Header from '@cloudscape-design/components/header'
import Table from '@cloudscape-design/components/table'
import Box from '@cloudscape-design/components/box'
import SpaceBetween from '@cloudscape-design/components/space-between'
import StatusIndicator from '@cloudscape-design/components/status-indicator'
import Button from '@cloudscape-design/components/button'
import Container from '@cloudscape-design/components/container'
import ExpandableSection from '@cloudscape-design/components/expandable-section'
import ColumnLayout from '@cloudscape-design/components/column-layout'
import Tabs from '@cloudscape-design/components/tabs'
import Prism from 'prismjs'
import 'prismjs/components/prism-json'
import 'prismjs/themes/prism.css'

const API = 'http://localhost:8000'

function JsonCode({ code }) {
  const ref = useRef(null)
  useEffect(() => {
    if (ref.current) Prism.highlightElement(ref.current)
  }, [code])
  return (
    <pre style={{ margin: 0, borderRadius: 6, overflow: 'auto', maxHeight: 500 }}>
      <code ref={ref} className="language-json">{code}</code>
    </pre>
  )
}

export default function StepFunctionsPage() {
  const [machines, setMachines] = useState([])
  const [selectedMachine, setSelectedMachine] = useState(null)
  const [machineDetail, setMachineDetail] = useState(null)
  const [executions, setExecutions] = useState([])
  const [loading, setLoading] = useState(true)

  const fetchMachines = () => {
    setLoading(true)
    fetch(`${API}/api/stepfunctions/state-machines`)
      .then(r => r.json())
      .then(data => { setMachines(data.stateMachines || []); setLoading(false) })
      .catch(() => setLoading(false))
  }

  useEffect(() => { fetchMachines() }, [])

  const openMachine = (arn) => {
    setSelectedMachine(arn)
    fetch(`${API}/api/stepfunctions/describe/${encodeURIComponent(arn)}`)
      .then(r => r.json())
      .then(data => setMachineDetail(data))
    fetch(`${API}/api/stepfunctions/executions/${encodeURIComponent(arn)}`)
      .then(r => r.json())
      .then(data => setExecutions(data.executions || []))
  }

  if (selectedMachine && machineDetail && !machineDetail.error) {
    const definition = machineDetail.definition || {}
    const states = definition.States || {}
    const stateList = Object.entries(states).map(([name, state]) => ({
      name,
      type: state.Type,
      next: state.Next || (state.End ? '(End)' : state.Default || '-'),
      comment: state.Comment || '-',
    }))

    return (
      <SpaceBetween size="l">
        <Header variant="h1" actions={<Button onClick={() => { setSelectedMachine(null); setMachineDetail(null) }}>Back</Button>}>
          {machineDetail.name}
        </Header>

        <ColumnLayout columns={3}>
          <Container>
            <Box variant="awsui-key-label">Status</Box>
            <StatusIndicator type="success">{machineDetail.status}</StatusIndicator>
          </Container>
          <Container>
            <Box variant="awsui-key-label">Start state</Box>
            <Box variant="awsui-value-large">{definition.StartAt}</Box>
          </Container>
          <Container>
            <Box variant="awsui-key-label">Total states</Box>
            <Box variant="awsui-value-large">{stateList.length}</Box>
          </Container>
        </ColumnLayout>

        <Tabs tabs={[
          {
            id: 'definition',
            label: 'Definition',
            content: (
              <SpaceBetween size="m">
                <Table
                  header={<Header variant="h2">States</Header>}
                  items={stateList}
                  columnDefinitions={[
                    { id: 'name', header: 'State', cell: item => <Box fontWeight="bold">{item.name}</Box> },
                    { id: 'type', header: 'Type', cell: item => (
                      <StatusIndicator type={
                        item.type === 'Task' ? 'info' :
                        item.type === 'Choice' ? 'warning' :
                        item.type === 'Pass' ? 'success' :
                        item.type === 'Succeed' ? 'success' :
                        item.type === 'Fail' ? 'error' : 'info'
                      }>{item.type}</StatusIndicator>
                    )},
                    { id: 'next', header: 'Next', cell: item => item.next },
                    { id: 'comment', header: 'Comment', cell: item => item.comment },
                  ]}
                />
                <ExpandableSection headerText="ASL JSON" variant="container">
                  <JsonCode code={JSON.stringify(definition, null, 2)} />
                </ExpandableSection>
              </SpaceBetween>
            ),
          },
          {
            id: 'executions',
            label: `Executions (${executions.length})`,
            content: (
              <Table
                header={<Header variant="h2">Executions</Header>}
                items={executions}
                columnDefinitions={[
                  { id: 'name', header: 'Name', cell: item => item.name },
                  { id: 'status', header: 'Status', cell: item => (
                    <StatusIndicator type={item.status === 'SUCCEEDED' ? 'success' : item.status === 'RUNNING' ? 'in-progress' : 'error'}>
                      {item.status}
                    </StatusIndicator>
                  )},
                  { id: 'started', header: 'Started', cell: item => item.startDate },
                ]}
                empty={<Box textAlign="center">No executions yet.</Box>}
              />
            ),
          },
          {
            id: 'flow',
            label: 'Flow',
            content: (
              <Container>
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4, padding: 16 }}>
                  {stateList.map((state, i) => (
                    <div key={state.name} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
                      <div style={{
                        border: `2px solid ${state.type === 'Choice' ? '#ff9900' : state.type === 'Task' ? '#0073bb' : state.type === 'Succeed' ? '#1d8102' : '#545b64'}`,
                        borderRadius: state.type === 'Choice' ? 0 : 8,
                        transform: state.type === 'Choice' ? 'rotate(45deg)' : 'none',
                        padding: state.type === 'Choice' ? 16 : '8px 24px',
                        background: '#fff',
                        minWidth: state.type === 'Choice' ? 0 : 160,
                        textAlign: 'center',
                      }}>
                        <span style={{ transform: state.type === 'Choice' ? 'rotate(-45deg)' : 'none', display: 'block', fontSize: 13 }}>
                          {state.name}
                        </span>
                      </div>
                      {i < stateList.length - 1 && (
                        <div style={{ width: 2, height: 24, background: '#545b64' }} />
                      )}
                    </div>
                  ))}
                </div>
              </Container>
            ),
          },
        ]} />
      </SpaceBetween>
    )
  }

  return (
    <SpaceBetween size="l">
      <Header variant="h1" actions={<Button onClick={fetchMachines} iconName="refresh">Refresh</Button>}>
        AWS Step Functions
      </Header>
      <Table
        header={<Header variant="h2">State machines</Header>}
        loading={loading}
        items={machines}
        columnDefinitions={[
          { id: 'name', header: 'Name', cell: item => (
            <Button variant="link" onClick={() => openMachine(item.stateMachineArn)}>{item.name}</Button>
          )},
          { id: 'arn', header: 'ARN', cell: item => <Box variant="code" fontSize="body-s">{item.stateMachineArn}</Box> },
          { id: 'created', header: 'Created', cell: item => item.creationDate },
        ]}
        empty={<Box textAlign="center">No state machines. Deploy one first.</Box>}
      />
    </SpaceBetween>
  )
}
