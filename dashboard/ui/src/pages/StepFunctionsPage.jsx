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
import Modal from '@cloudscape-design/components/modal'
import Alert from '@cloudscape-design/components/alert'
import Badge from '@cloudscape-design/components/badge'
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

const sleep = (ms) => new Promise(r => setTimeout(r, ms))

export default function StepFunctionsPage() {
  const [machines, setMachines] = useState([])
  const [templates, setTemplates] = useState([])
  const [selectedMachine, setSelectedMachine] = useState(null)
  const [machineDetail, setMachineDetail] = useState(null)
  const [executions, setExecutions] = useState([])
  const [loading, setLoading] = useState(true)
  const [showCreate, setShowCreate] = useState(false)
  const [creating, setCreating] = useState(null)
  const [running, setRunning] = useState(false)
  const [runResult, setRunResult] = useState(null)

  const fetchMachines = () => {
    setLoading(true)
    fetch(`${API}/api/stepfunctions/state-machines`)
      .then(r => r.json())
      .then(data => { setMachines(data.stateMachines || []); setLoading(false) })
      .catch(() => setLoading(false))
  }

  useEffect(() => {
    fetchMachines()
    fetch(`${API}/api/stepfunctions/templates`).then(r => r.json()).then(d => setTemplates(d.templates || []))
  }, [])

  const openMachine = (arn) => {
    setSelectedMachine(arn)
    setRunResult(null)
    fetch(`${API}/api/stepfunctions/describe/${encodeURIComponent(arn)}`)
      .then(r => r.json())
      .then(data => setMachineDetail(data))
    fetch(`${API}/api/stepfunctions/executions/${encodeURIComponent(arn)}`)
      .then(r => r.json())
      .then(data => setExecutions(data.executions || []))
  }

  const createFromTemplate = (id) => {
    setCreating(id)
    fetch(`${API}/api/stepfunctions/create`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ templateId: id }),
    })
      .then(r => r.json())
      .then(d => {
        setCreating(null); setShowCreate(false); fetchMachines()
        if (d.stateMachineArn) openMachine(d.stateMachineArn)
      })
      .catch(() => setCreating(null))
  }

  const runExecution = async (tpl) => {
    setRunning(true); setRunResult(null)
    try {
      const started = await fetch(`${API}/api/stepfunctions/start`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ stateMachineArn: selectedMachine, testCase: tpl.testCase, input: tpl.input || {} }),
      }).then(r => r.json())
      if (started.error) { setRunResult({ status: 'FAILED', error: started.error, steps: [] }); setRunning(false); return }
      const exArn = started.executionArn
      let result = null
      for (let i = 0; i < 25; i++) {
        result = await fetch(`${API}/api/stepfunctions/execution/${encodeURIComponent(exArn)}`).then(r => r.json())
        if (result.status && result.status !== 'RUNNING') break
        await sleep(500)
      }
      setRunResult(result)
      fetch(`${API}/api/stepfunctions/executions/${encodeURIComponent(selectedMachine)}`)
        .then(r => r.json()).then(data => setExecutions(data.executions || []))
    } catch (e) {
      setRunResult({ status: 'FAILED', error: String(e), steps: [] })
    }
    setRunning(false)
  }

  const stepStatus = (s) => s === 'SUCCEEDED' ? 'success' : s === 'FAILED' ? 'error' : 'in-progress'

  if (selectedMachine && machineDetail && !machineDetail.error) {
    const definition = machineDetail.definition || {}
    const states = definition.States || {}
    const stateList = Object.entries(states).map(([name, state]) => ({
      name,
      type: state.Type,
      next: state.Next || (state.End ? '(End)' : state.Default || '-'),
      comment: state.Comment || '-',
    }))
    const tpl = templates.find(t => t.name === machineDetail.name)
    const canRun = tpl && tpl.runnable

    return (
      <SpaceBetween size="l">
        <Header
          variant="h1"
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              {canRun && (
                <Button variant="primary" iconName="play" loading={running} onClick={() => runExecution(tpl)}>
                  Run (mock)
                </Button>
              )}
              <Button onClick={() => { setSelectedMachine(null); setMachineDetail(null); setRunResult(null) }}>Back</Button>
            </SpaceBetween>
          }
        >
          {machineDetail.name}
        </Header>

        {tpl && !tpl.runnable && (
          <Alert type="info" header="Inspect only on the local engine">{tpl.note}</Alert>
        )}

        {runResult && (
          <Container header={<Header variant="h2">Run result</Header>}>
            <SpaceBetween size="m">
              <StatusIndicator type={stepStatus(runResult.status)}>{runResult.status}</StatusIndicator>
              {runResult.error && <Alert type="error">{runResult.cause || runResult.error}</Alert>}
              {(runResult.steps || []).map(s => (
                <Box key={s.name}>
                  <StatusIndicator type={stepStatus(s.status)}>
                    <Box variant="span" fontWeight="bold">{s.name}</Box> <Box variant="span" color="text-status-inactive">({s.type})</Box>
                  </StatusIndicator>
                </Box>
              ))}
              {runResult.output && (
                <ExpandableSection headerText="Output" variant="container">
                  <JsonCode code={JSON.stringify(JSON.parse(runResult.output), null, 2)} />
                </ExpandableSection>
              )}
            </SpaceBetween>
          </Container>
        )}

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
      <Header
        variant="h1"
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button iconName="add-plus" onClick={() => setShowCreate(true)}>Create from template</Button>
            <Button onClick={fetchMachines} iconName="refresh">Refresh</Button>
          </SpaceBetween>
        }
      >
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
        empty={<Box textAlign="center">No state machines yet. Create one from a template.</Box>}
      />

      <Modal
        visible={showCreate}
        onDismiss={() => setShowCreate(false)}
        header="Create a state machine from a template"
        size="large"
      >
        <SpaceBetween size="m">
          <Box color="text-body-secondary">
            ML-focused credit-risk workflows adapted from aws-samples/credit-risk-modeling-on-aws.
            Runnable templates execute locally via Step Functions Local mock mode.
          </Box>
          {templates.map(t => (
            <Container
              key={t.id}
              header={
                <Header
                  variant="h3"
                  actions={
                    <Button
                      variant="primary"
                      loading={creating === t.id}
                      onClick={() => createFromTemplate(t.id)}
                    >
                      Create
                    </Button>
                  }
                >
                  {t.name}{' '}
                  {t.runnable
                    ? <Badge color="green">runnable</Badge>
                    : <Badge color="grey">inspect only</Badge>}
                </Header>
              }
            >
              <Box>{t.comment}</Box>
              {!t.runnable && t.note && <Box color="text-status-inactive" fontSize="body-s" padding={{ top: 'xs' }}>{t.note}</Box>}
            </Container>
          ))}
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  )
}
