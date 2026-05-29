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
import Select from '@cloudscape-design/components/select'
import Alert from '@cloudscape-design/components/alert'
import Badge from '@cloudscape-design/components/badge'
import StatusIndicator from '@cloudscape-design/components/status-indicator'
import ColumnLayout from '@cloudscape-design/components/column-layout'
import CodeEditor from '../components/CodeEditor'

const API = 'http://localhost:8000'

// A ready-to-run feature-flags config: an eq guard + an A/B split. The split is
// the exact AWS algorithm (fnv1a_32(by+seed) % 100000 / 1000 < pct), so the
// evaluator below matches what the real AppConfig agent would resolve.
const STARTER_FLAGS = JSON.stringify({
  version: '1',
  values: {
    new_checkout: {
      enabled: true,
      _variants: [
        {
          name: 'treatment', enabled: true,
          attributeValues: { discount: 0.1 },
          rule: '(and (eq $tier "vip") (split by::$userId pct::50 seed::"co"))',
        },
        { name: 'control', enabled: true },
      ],
    },
  },
}, null, 2)

const profileOption = (p) => ({ value: p.id, label: p.name })

export default function AppConfigPage() {
  const [apps, setApps] = useState([])
  const [selectedApp, setSelectedApp] = useState(null)

  // Effect bodies only kick off async work — every setState lands inside .then().
  const loadApps = () =>
    fetch(`${API}/api/appconfig/applications`).then(r => r.json()).then(d => {
      setApps(d.applications || [])
      setSelectedApp(prev => prev || (d.applications?.[0] ?? null))
    })
  useEffect(() => { loadApps() }, [])

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="AWS AppConfig — applications, environments, configuration profiles, hosted versions and deployments. The Flag evaluator resolves feature-flag variants exactly like the AWS AppConfig agent (incl. the A/B split)."
      >
        AWS AppConfig
      </Header>
      <Tabs tabs={[
        { id: 'apps', label: 'Applications',
          content: <ApplicationsTab apps={apps} selectedApp={selectedApp}
            setSelectedApp={setSelectedApp} refreshApps={loadApps} /> },
        { id: 'config', label: 'Configurations',
          content: <ConfigurationsTab app={selectedApp} /> },
        { id: 'eval', label: 'Flag evaluator',
          content: <EvaluatorTab app={selectedApp} /> },
      ]} />
    </SpaceBetween>
  )
}

function ApplicationsTab({ apps, selectedApp, setSelectedApp, refreshApps }) {
  const [envs, setEnvs] = useState([])
  const [profiles, setProfiles] = useState([])
  const [modal, setModal] = useState(null)  // 'app' | 'env' | 'profile'

  const appId = selectedApp?.id
  const loadChildren = (id) => {
    if (!id) return
    fetch(`${API}/api/appconfig/applications/${id}/environments`)
      .then(r => r.json()).then(d => setEnvs(d.environments || []))
    fetch(`${API}/api/appconfig/applications/${id}/profiles`)
      .then(r => r.json()).then(d => setProfiles(d.profiles || []))
  }
  useEffect(() => { loadChildren(appId) }, [appId])

  return (
    <SpaceBetween size="l">
      <Container header={
        <Header variant="h2" counter={`(${apps.length})`}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button iconName="add-plus" onClick={() => setModal('app')}>Create application</Button>
              <Button iconName="refresh" onClick={refreshApps}>Refresh</Button>
            </SpaceBetween>
          }>
          Applications
        </Header>
      }>
        <Table
          items={apps}
          selectionType="single"
          selectedItems={apps.filter(a => a.id === appId)}
          onSelectionChange={({ detail }) => setSelectedApp(detail.selectedItems[0])}
          columnDefinitions={[
            { id: 'name', header: 'Name', cell: i => <Box variant="strong">{i.name}</Box> },
            { id: 'id', header: 'Id', cell: i => <Box variant="code" fontSize="body-s">{i.id}</Box> },
            { id: 'environments', header: 'Environments', cell: i => i.environments },
            { id: 'profiles', header: 'Profiles', cell: i => i.profiles },
            { id: 'description', header: 'Description', cell: i => i.description || '—' },
          ]}
          empty={<Box textAlign="center">No applications yet.</Box>}
        />
      </Container>

      <ColumnLayout columns={2}>
        <Container header={
          <Header variant="h2" counter={`(${envs.length})`}
            actions={<Button iconName="add-plus" disabled={!appId}
              onClick={() => setModal('env')}>Create</Button>}
            description={selectedApp ? `Environments in ${selectedApp.name}` : 'Select an application'}>
            Environments
          </Header>
        }>
          <Table
            items={envs}
            columnDefinitions={[
              { id: 'name', header: 'Name', cell: i => <Box variant="strong">{i.name}</Box> },
              { id: 'state', header: 'State', cell: i => <Badge color="green">{i.state || 'ReadyForDeployment'}</Badge> },
            ]}
            empty={<Box textAlign="center">No environments.</Box>}
          />
        </Container>

        <Container header={
          <Header variant="h2" counter={`(${profiles.length})`}
            actions={<Button iconName="add-plus" disabled={!appId}
              onClick={() => setModal('profile')}>Create</Button>}
            description={selectedApp ? `Profiles in ${selectedApp.name}` : 'Select an application'}>
            Configuration profiles
          </Header>
        }>
          <Table
            items={profiles}
            columnDefinitions={[
              { id: 'name', header: 'Name', cell: i => <Box variant="strong">{i.name}</Box> },
              { id: 'type', header: 'Type', cell: i => (
                <Badge color={i.type === 'AWS.AppConfig.FeatureFlags' ? 'blue' : 'grey'}>
                  {i.type === 'AWS.AppConfig.FeatureFlags' ? 'FeatureFlags' : 'Freeform'}
                </Badge>
              )},
            ]}
            empty={<Box textAlign="center">No profiles.</Box>}
          />
        </Container>
      </ColumnLayout>

      {modal === 'app' && (
        <CreateModal title="Create application" url={`${API}/api/appconfig/applications`}
          fields={[{ key: 'name', label: 'Name' }, { key: 'description', label: 'Description (optional)' }]}
          onClose={() => setModal(null)} onDone={refreshApps} />
      )}
      {modal === 'env' && (
        <CreateModal title="Create environment"
          url={`${API}/api/appconfig/applications/${appId}/environments`}
          fields={[{ key: 'name', label: 'Name' }, { key: 'description', label: 'Description (optional)' }]}
          onClose={() => setModal(null)} onDone={() => loadChildren(appId)} />
      )}
      {modal === 'profile' && (
        <CreateProfileModal appId={appId}
          onClose={() => setModal(null)} onDone={() => loadChildren(appId)} />
      )}
    </SpaceBetween>
  )
}

function CreateModal({ title, url, fields, onClose, onDone }) {
  const [values, setValues] = useState({})
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const set = (k, v) => setValues({ ...values, [k]: v })

  const submit = async () => {
    setBusy(true); setErr(null)
    const r = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(values),
    }).then(r => r.json())
    setBusy(false)
    if (r.error) { setErr(r.error); return }
    onDone(); onClose()
  }

  return (
    <Modal visible onDismiss={onClose} header={title} size="medium"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={onClose}>Cancel</Button>
            <Button variant="primary" loading={busy} disabled={!values.name} onClick={submit}>Create</Button>
          </SpaceBetween>
        </Box>
      }>
      <SpaceBetween size="m">
        {err && <Alert type="error">{err}</Alert>}
        {fields.map(f => (
          <FormField key={f.key} label={f.label}>
            <Input value={values[f.key] || ''} onChange={({ detail }) => set(f.key, detail.value)} />
          </FormField>
        ))}
      </SpaceBetween>
    </Modal>
  )
}

function CreateProfileModal({ appId, onClose, onDone }) {
  const [name, setName] = useState('')
  const [type, setType] = useState({ value: 'AWS.AppConfig.FeatureFlags', label: 'FeatureFlags' })
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  const submit = async () => {
    setBusy(true); setErr(null)
    const r = await fetch(`${API}/api/appconfig/applications/${appId}/profiles`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, type: type.value }),
    }).then(r => r.json())
    setBusy(false)
    if (r.error) { setErr(r.error); return }
    onDone(); onClose()
  }

  return (
    <Modal visible onDismiss={onClose} header="Create configuration profile" size="medium"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={onClose}>Cancel</Button>
            <Button variant="primary" loading={busy} disabled={!name} onClick={submit}>Create</Button>
          </SpaceBetween>
        </Box>
      }>
      <SpaceBetween size="m">
        {err && <Alert type="error">{err}</Alert>}
        <FormField label="Name"><Input value={name} onChange={({ detail }) => setName(detail.value)} /></FormField>
        <FormField label="Type">
          <Select selectedOption={type} onChange={({ detail }) => setType(detail.selectedOption)}
            options={[
              { value: 'AWS.AppConfig.FeatureFlags', label: 'FeatureFlags' },
              { value: 'AWS.Freeform', label: 'Freeform' },
            ]} />
        </FormField>
      </SpaceBetween>
    </Modal>
  )
}

function ConfigurationsTab({ app }) {
  const [profiles, setProfiles] = useState([])
  const [profile, setProfile] = useState(null)
  const [versions, setVersions] = useState([])
  const [content, setContent] = useState(STARTER_FLAGS)
  const [creating, setCreating] = useState(false)
  const [err, setErr] = useState(null)
  const [deployOpen, setDeployOpen] = useState(false)

  // Fetch profiles + pick a default when the app changes — all setState in .then().
  useEffect(() => {
    if (!app?.id) return
    fetch(`${API}/api/appconfig/applications/${app.id}/profiles`)
      .then(r => r.json()).then(d => {
        setProfiles(d.profiles || [])
        setProfile(d.profiles?.[0] ? profileOption(d.profiles[0]) : null)
      })
  }, [app?.id])

  const loadVersions = () => {
    if (!app?.id || !profile) return
    fetch(`${API}/api/appconfig/applications/${app.id}/profiles/${profile.value}/versions`)
      .then(r => r.json()).then(d => setVersions(d.versions || []))
  }
  // Inline (not loadVersions()) so the dep array stays exhaustive — same effect body.
  useEffect(() => {
    if (!app?.id || !profile) return
    fetch(`${API}/api/appconfig/applications/${app.id}/profiles/${profile.value}/versions`)
      .then(r => r.json()).then(d => setVersions(d.versions || []))
  }, [app?.id, profile])

  const viewVersion = (n) => {
    fetch(`${API}/api/appconfig/applications/${app.id}/profiles/${profile.value}/versions/${n}`)
      .then(r => r.json()).then(d => { if (d.content != null) setContent(d.content) })
  }

  const save = async () => {
    setCreating(true); setErr(null)
    const r = await fetch(
      `${API}/api/appconfig/applications/${app.id}/profiles/${profile.value}/versions`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
      }).then(r => r.json())
    setCreating(false)
    if (r.error) { setErr(r.error); return }
    loadVersions()
  }

  if (!app) return <Box padding="m">Select an application on the Applications tab.</Box>

  return (
    <SpaceBetween size="l">
      <Container header={
        <Header variant="h2"
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button iconName="refresh" onClick={loadVersions}>Refresh</Button>
              <Button disabled={!profile || !versions.length}
                onClick={() => setDeployOpen(true)}>Deploy…</Button>
            </SpaceBetween>
          }
          description={`Hosted configuration versions for ${app.name}`}>
          Versions
        </Header>
      }>
        <SpaceBetween size="m">
          <FormField label="Profile">
            <Select selectedOption={profile} onChange={({ detail }) => setProfile(detail.selectedOption)}
              options={profiles.map(profileOption)} placeholder="Select a profile" />
          </FormField>
          <Table
            items={versions}
            columnDefinitions={[
              { id: 'n', header: 'Version', cell: i => <Box variant="strong">{i.versionNumber}</Box> },
              { id: 'ct', header: 'Content-Type', cell: i => <Box fontSize="body-s">{i.contentType}</Box> },
              { id: 'view', header: '', cell: i => (
                <Button variant="inline-link" onClick={() => viewVersion(i.versionNumber)}>View</Button>
              )},
            ]}
            empty={<Box textAlign="center">No versions yet — write one below.</Box>}
          />
        </SpaceBetween>
      </Container>

      <Container header={
        <Header variant="h2"
          actions={<Button variant="primary" iconName="upload" loading={creating}
            disabled={!profile} onClick={save}>Save as new version</Button>}
          description="Edit then save to create a new hosted version. FeatureFlags configs use the AWS `values` schema.">
          Content (JSON)
        </Header>
      }>
        <SpaceBetween size="s">
          {err && <Alert type="error">{err}</Alert>}
          <CodeEditor value={content} onChange={setContent} language="json" rows={18} />
        </SpaceBetween>
      </Container>

      {deployOpen && (
        <DeployModal app={app} profile={profile} versions={versions}
          onClose={() => setDeployOpen(false)} />
      )}
    </SpaceBetween>
  )
}

function DeployModal({ app, profile, versions, onClose }) {
  const [envs, setEnvs] = useState([])
  const [strategies, setStrategies] = useState([])
  const [env, setEnv] = useState(null)
  const [strategy, setStrategy] = useState(null)
  const [version, setVersion] = useState(
    versions[0] ? { value: String(versions[0].versionNumber), label: `v${versions[0].versionNumber}` } : null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const [err, setErr] = useState(null)

  useEffect(() => {
    fetch(`${API}/api/appconfig/applications/${app.id}/environments`)
      .then(r => r.json()).then(d => setEnvs(d.environments || []))
    fetch(`${API}/api/appconfig/strategies`).then(r => r.json()).then(d => {
      setStrategies(d.strategies || [])
      const all = (d.strategies || []).find(s => s.id === 'AppConfig.AllAtOnce')
      setStrategy(all ? { value: all.id, label: all.name } : null)
    })
  }, [app.id])

  const deploy = async () => {
    setBusy(true); setErr(null); setResult(null)
    const r = await fetch(
      `${API}/api/appconfig/applications/${app.id}/environments/${env.value}/deployments`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          profileId: profile.value, version: version.value, strategyId: strategy.value,
        }),
      }).then(r => r.json())
    setBusy(false)
    if (r.error) { setErr(r.error); return }
    setResult(r)
  }

  return (
    <Modal visible onDismiss={onClose} header="Start deployment" size="medium"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={onClose}>Close</Button>
            <Button variant="primary" loading={busy}
              disabled={!env || !version || !strategy} onClick={deploy}>Deploy</Button>
          </SpaceBetween>
        </Box>
      }>
      <SpaceBetween size="m">
        {err && <Alert type="error">{err}</Alert>}
        {result && (
          <Alert type="success">
            Deployment #{result.deploymentNumber} — <StatusIndicator type="success">{result.state}</StatusIndicator>
          </Alert>
        )}
        <FormField label="Environment">
          <Select selectedOption={env} onChange={({ detail }) => setEnv(detail.selectedOption)}
            options={envs.map(e => ({ value: e.id, label: e.name }))} placeholder="Select environment" />
        </FormField>
        <FormField label="Version">
          <Select selectedOption={version} onChange={({ detail }) => setVersion(detail.selectedOption)}
            options={versions.map(v => ({ value: String(v.versionNumber), label: `v${v.versionNumber}` }))} />
        </FormField>
        <FormField label="Deployment strategy">
          <Select selectedOption={strategy} onChange={({ detail }) => setStrategy(detail.selectedOption)}
            options={strategies.map(s => ({ value: s.id, label: s.name }))} />
        </FormField>
      </SpaceBetween>
    </Modal>
  )
}

function EvaluatorTab({ app }) {
  const [profiles, setProfiles] = useState([])
  const [profile, setProfile] = useState(null)
  const [versions, setVersions] = useState([])
  const [version, setVersion] = useState(null)
  const [context, setContext] = useState(JSON.stringify({ tier: 'vip', userId: 'u-42' }, null, 2))
  const [flags, setFlags] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  useEffect(() => {
    if (!app?.id) return
    fetch(`${API}/api/appconfig/applications/${app.id}/profiles`)
      .then(r => r.json()).then(d => {
        setProfiles(d.profiles || [])
        setProfile(d.profiles?.[0] ? profileOption(d.profiles[0]) : null)
      })
  }, [app?.id])

  useEffect(() => {
    if (!app?.id || !profile) return
    fetch(`${API}/api/appconfig/applications/${app.id}/profiles/${profile.value}/versions`)
      .then(r => r.json()).then(d => {
        setVersions(d.versions || [])
        setVersion(d.versions?.[0]
          ? { value: String(d.versions[0].versionNumber), label: `v${d.versions[0].versionNumber}` } : null)
      })
  }, [app?.id, profile])

  const evaluate = async () => {
    setBusy(true); setErr(null); setFlags(null)
    let ctx
    try { ctx = JSON.parse(context) } catch (e) { setErr(`Context is not valid JSON: ${e}`); setBusy(false); return }
    const r = await fetch(`${API}/api/appconfig/evaluate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        applicationId: app.id, profileId: profile.value, version: version.value, context: ctx,
      }),
    }).then(r => r.json())
    setBusy(false)
    if (r.error) { setErr(r.error); return }
    setFlags(r.flags || {})
  }

  if (!app) return <Box padding="m">Select an application on the Applications tab.</Box>

  const rows = flags ? Object.entries(flags).map(([name, f]) => ({
    name,
    variant: f._variant || '—',
    attributes: Object.fromEntries(
      Object.entries(f).filter(([k]) => !k.startsWith('_') && k !== 'enabled' && k !== 'name')),
  })) : []

  return (
    <SpaceBetween size="l">
      <Container header={
        <Header variant="h2"
          description="Resolve feature-flag variants for a context — the AppConfig agent's job. The A/B `split` uses the exact AWS algorithm, so a userId lands in the same bucket here as in real AWS.">
          Flag evaluator
        </Header>
      }>
        <SpaceBetween size="m">
          <ColumnLayout columns={3}>
            <FormField label="Profile">
              <Select selectedOption={profile} onChange={({ detail }) => setProfile(detail.selectedOption)}
                options={profiles.map(profileOption)} placeholder="Profile" />
            </FormField>
            <FormField label="Version">
              <Select selectedOption={version} onChange={({ detail }) => setVersion(detail.selectedOption)}
                options={versions.map(v => ({ value: String(v.versionNumber), label: `v${v.versionNumber}` }))}
                placeholder="Version" />
            </FormField>
            <Box padding={{ top: 'l' }}>
              <Button variant="primary" iconName="search" loading={busy}
                disabled={!profile || !version} onClick={evaluate}>Evaluate</Button>
            </Box>
          </ColumnLayout>
          <FormField label="Request context (JSON)">
            <CodeEditor value={context} onChange={setContext} language="json" rows={8} />
          </FormField>
          {err && <Alert type="error">{err}</Alert>}
        </SpaceBetween>
      </Container>

      {flags && (
        <Container header={<Header variant="h2">Resolved flags</Header>}>
          <Table
            items={rows}
            columnDefinitions={[
              { id: 'name', header: 'Flag', cell: i => <Box variant="strong">{i.name}</Box> },
              { id: 'variant', header: 'Variant', cell: i => (
                <Badge color={i.variant === 'control' || i.variant === '—' ? 'grey' : 'green'}>{i.variant}</Badge>
              )},
              { id: 'attrs', header: 'Attributes', cell: i => (
                <Box variant="code" fontSize="body-s">
                  {Object.keys(i.attributes).length ? JSON.stringify(i.attributes) : '—'}
                </Box>
              )},
            ]}
            empty={<Box textAlign="center">No enabled flags resolved.</Box>}
          />
        </Container>
      )}
    </SpaceBetween>
  )
}
