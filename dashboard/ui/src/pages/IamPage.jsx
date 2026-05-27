import { useState, useEffect } from 'react'
import Header from '@cloudscape-design/components/header'
import Table from '@cloudscape-design/components/table'
import Box from '@cloudscape-design/components/box'
import SpaceBetween from '@cloudscape-design/components/space-between'
import Button from '@cloudscape-design/components/button'
import Container from '@cloudscape-design/components/container'
import StatusIndicator from '@cloudscape-design/components/status-indicator'
import Modal from '@cloudscape-design/components/modal'
import FormField from '@cloudscape-design/components/form-field'
import Input from '@cloudscape-design/components/input'
import Textarea from '@cloudscape-design/components/textarea'
import Select from '@cloudscape-design/components/select'
import Alert from '@cloudscape-design/components/alert'

const API = 'http://localhost:8000'

const TRUST_TEMPLATE = JSON.stringify({
  Version: '2012-10-17',
  Statement: [{ Effect: 'Allow', Action: 'sts:AssumeRole', Principal: { AWS: 'arn:aws:iam::222222222222:root' } }],
}, null, 2)

const POLICY_TEMPLATE = JSON.stringify({
  Version: '2012-10-17',
  Statement: [{ Effect: 'Allow', Action: ['s3:GetObject'], Resource: 'arn:aws:s3:::credit-data/*' }],
}, null, 2)

const post = (path, body) =>
  fetch(`${API}${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(r => r.json())

export default function IamPage() {
  const [ov, setOv] = useState({ users: [], roles: [], policies: [] })
  const [modal, setModal] = useState(null) // 'role' | 'policy' | 'attach'
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  // create-role / create-policy / attach forms
  const [roleForm, setRoleForm] = useState({ name: '', trust: TRUST_TEMPLATE })
  const [polForm, setPolForm] = useState({ name: '', doc: POLICY_TEMPLATE })
  const [attach, setAttach] = useState({ policyArn: '', roleName: null })

  // assume-role + simulate panels
  const [assume, setAssume] = useState({ roleArn: null, principalArn: 'arn:aws:iam::222222222222:role/app' })
  const [assumeResult, setAssumeResult] = useState(null)
  const [sim, setSim] = useState({ principalArn: '', action: 's3:GetObject', resource: 'arn:aws:s3:::credit-data/loans.csv' })
  const [simResult, setSimResult] = useState(null)

  const refresh = () => fetch(`${API}/api/iam/overview`).then(r => r.json()).then(setOv)
  useEffect(refresh, [])

  const roleOpts = ov.roles.map(r => ({ label: r.name, value: r.arn }))

  const createRole = () => {
    let trustPolicy
    try { trustPolicy = JSON.parse(roleForm.trust) } catch (e) { setErr(`Trust policy JSON: ${e.message}`); return }
    setBusy(true); setErr(null)
    post('/api/iam/roles', { name: roleForm.name, trustPolicy }).then(d => {
      setBusy(false); if (d.error) return setErr(d.error); setModal(null); refresh()
    })
  }
  const createPolicy = () => {
    let document
    try { document = JSON.parse(polForm.doc) } catch (e) { setErr(`Policy JSON: ${e.message}`); return }
    setBusy(true); setErr(null)
    post('/api/iam/policies', { name: polForm.name, document }).then(d => {
      setBusy(false); if (d.error) return setErr(d.error); setModal(null); refresh()
    })
  }
  const doAttach = () => {
    setBusy(true); setErr(null)
    post('/api/iam/attach', { policyArn: attach.policyArn, roleName: attach.roleName?.value }).then(d => {
      setBusy(false); if (d.error) return setErr(d.error); setModal(null)
    })
  }
  const doAssume = () => {
    if (!assume.roleArn) { setAssumeResult({ error: 'Pick a role' }); return }
    post('/api/iam/assume-role', { roleArn: assume.roleArn.value, principalArn: assume.principalArn }).then(setAssumeResult)
  }
  const doSimulate = () => post('/api/iam/simulate', sim).then(setSimResult)

  const decisionIndicator = (d) =>
    <StatusIndicator type={d === 'Allow' ? 'success' : 'error'}>{d}</StatusIndicator>

  return (
    <SpaceBetween size="l">
      <Header variant="h1" actions={<Button iconName="refresh" onClick={refresh}>Refresh</Button>}
        description="IAM/STS control plane via moto; trust + authorization decided by oblako's policy evaluator. Cross-account is expressed in ARNs/trust policies.">
        IAM
      </Header>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20, alignItems: 'stretch' }}>
        <Container fitHeight header={<Header variant="h2" description="Trust-evaluated; cross-account principals welcome">Assume role</Header>}>
          <SpaceBetween size="s">
            <FormField label="Role"><Select selectedOption={assume.roleArn} options={roleOpts}
              onChange={({ detail }) => setAssume(a => ({ ...a, roleArn: detail.selectedOption }))} placeholder="Select a role" /></FormField>
            <FormField label="Calling principal ARN"><Input value={assume.principalArn}
              onChange={({ detail }) => setAssume(a => ({ ...a, principalArn: detail.value }))} /></FormField>
            <Button onClick={doAssume}>Assume role</Button>
            {assumeResult && (assumeResult.error
              ? <Alert type="error">{assumeResult.error}</Alert>
              : assumeResult.allowed
                ? <Alert type="success" header="Allowed">Assumed <Box variant="code" fontSize="body-s">{assumeResult.assumedRoleArn}</Box></Alert>
                : <Alert type="error" header="Denied">{assumeResult.reason}</Alert>)}
          </SpaceBetween>
        </Container>

        <Container fitHeight header={<Header variant="h2" description="Does this principal's policies allow the action?">Simulate access</Header>}>
          <SpaceBetween size="s">
            <FormField label="Principal ARN (user or role)"><Input value={sim.principalArn}
              onChange={({ detail }) => setSim(s => ({ ...s, principalArn: detail.value }))} placeholder="arn:aws:iam::123456789012:role/scorer" /></FormField>
            <FormField label="Action"><Input value={sim.action} onChange={({ detail }) => setSim(s => ({ ...s, action: detail.value }))} /></FormField>
            <FormField label="Resource"><Input value={sim.resource} onChange={({ detail }) => setSim(s => ({ ...s, resource: detail.value }))} /></FormField>
            <Button onClick={doSimulate}>Simulate</Button>
            {simResult && (simResult.error
              ? <Alert type="error">{simResult.error}</Alert>
              : <Box>Decision: {decisionIndicator(simResult.decision)}</Box>)}
          </SpaceBetween>
        </Container>
      </div>

      <Table
        header={<Header variant="h2" counter={`(${ov.roles.length})`}
          actions={<Button iconName="add-plus" onClick={() => { setErr(null); setModal('role') }}>Create role</Button>}>Roles</Header>}
        items={ov.roles}
        columnDefinitions={[
          { id: 'name', header: 'Name', cell: r => <Box fontWeight="bold">{r.name}</Box> },
          { id: 'arn', header: 'ARN', cell: r => <Box variant="code" fontSize="body-s">{r.arn}</Box> },
        ]}
        empty={<Box textAlign="center">No roles yet.</Box>}
      />

      <Table
        header={<Header variant="h2" counter={`(${ov.policies.length})`}
          actions={<Button iconName="add-plus" onClick={() => { setErr(null); setModal('policy') }}>Create policy</Button>}>Customer-managed policies</Header>}
        items={ov.policies}
        columnDefinitions={[
          { id: 'name', header: 'Name', cell: p => <Box fontWeight="bold">{p.name}</Box> },
          { id: 'arn', header: 'ARN', cell: p => <Box variant="code" fontSize="body-s">{p.arn}</Box> },
          { id: 'attach', header: '', cell: p => <Button variant="link" onClick={() => { setErr(null); setAttach({ policyArn: p.arn, roleName: null }); setModal('attach') }}>Attach to role</Button> },
        ]}
        empty={<Box textAlign="center">No customer-managed policies yet.</Box>}
      />

      <Table
        header={<Header variant="h2" counter={`(${ov.users.length})`}>Users</Header>}
        items={ov.users}
        columnDefinitions={[
          { id: 'name', header: 'Name', cell: u => <Box fontWeight="bold">{u.name}</Box> },
          { id: 'arn', header: 'ARN', cell: u => <Box variant="code" fontSize="body-s">{u.arn}</Box> },
        ]}
        empty={<Box textAlign="center">No users yet.</Box>}
      />

      {/* Create role */}
      <Modal visible={modal === 'role'} onDismiss={() => setModal(null)} header="Create role" size="medium"
        footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => setModal(null)}>Cancel</Button>
          <Button variant="primary" loading={busy} onClick={createRole}>Create</Button>
        </SpaceBetween></Box>}>
        <SpaceBetween size="m">
          {err && <Alert type="error">{err}</Alert>}
          <FormField label="Role name"><Input value={roleForm.name} onChange={({ detail }) => setRoleForm(f => ({ ...f, name: detail.value }))} placeholder="cross-account-scorer" /></FormField>
          <FormField label="Trust policy"><Textarea value={roleForm.trust} onChange={({ detail }) => setRoleForm(f => ({ ...f, trust: detail.value }))} rows={10} spellcheck={false} /></FormField>
        </SpaceBetween>
      </Modal>

      {/* Create policy */}
      <Modal visible={modal === 'policy'} onDismiss={() => setModal(null)} header="Create customer-managed policy" size="medium"
        footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => setModal(null)}>Cancel</Button>
          <Button variant="primary" loading={busy} onClick={createPolicy}>Create</Button>
        </SpaceBetween></Box>}>
        <SpaceBetween size="m">
          {err && <Alert type="error">{err}</Alert>}
          <FormField label="Policy name"><Input value={polForm.name} onChange={({ detail }) => setPolForm(f => ({ ...f, name: detail.value }))} placeholder="s3-read-credit-data" /></FormField>
          <FormField label="Policy document"><Textarea value={polForm.doc} onChange={({ detail }) => setPolForm(f => ({ ...f, doc: detail.value }))} rows={10} spellcheck={false} /></FormField>
        </SpaceBetween>
      </Modal>

      {/* Attach */}
      <Modal visible={modal === 'attach'} onDismiss={() => setModal(null)} header="Attach policy to role"
        footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => setModal(null)}>Cancel</Button>
          <Button variant="primary" loading={busy} onClick={doAttach}>Attach</Button>
        </SpaceBetween></Box>}>
        <SpaceBetween size="m">
          {err && <Alert type="error">{err}</Alert>}
          <Box variant="code" fontSize="body-s">{attach.policyArn}</Box>
          <FormField label="Role"><Select selectedOption={attach.roleName} options={ov.roles.map(r => ({ label: r.name, value: r.name }))}
            onChange={({ detail }) => setAttach(a => ({ ...a, roleName: detail.selectedOption }))} placeholder="Select a role" /></FormField>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  )
}
