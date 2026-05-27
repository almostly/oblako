import { useState } from 'react'
import AppLayout from '@cloudscape-design/components/app-layout'
import SideNavigation from '@cloudscape-design/components/side-navigation'
import TopNavigation from '@cloudscape-design/components/top-navigation'
import ServicesPage from './pages/ServicesPage'
import S3Page from './pages/S3Page'
import StepFunctionsPage from './pages/StepFunctionsPage'
import CloudFormationPage from './pages/CloudFormationPage'
import BedrockPage from './pages/BedrockPage'
import RedshiftPage from './pages/RedshiftPage'

import DynamoDBPage from './pages/DynamoDBPage'
import SageMakerPage from './pages/SageMakerPage'
import NotebookPage from './pages/NotebookPage'
import RdsPage from './pages/RdsPage'
import IamPage from './pages/IamPage'

const NAV_ITEMS = [
  { type: 'link', text: 'Services', href: '#services' },
  { type: 'link', text: 'Notebook', href: '#notebook' },
  { type: 'divider' },
  { type: 'link', text: 'Bedrock', href: '#bedrock' },
  { type: 'link', text: 'SageMaker', href: '#sagemaker' },
  { type: 'link', text: 'S3', href: '#s3' },
  { type: 'link', text: 'DynamoDB', href: '#dynamodb' },
  { type: 'link', text: 'Step Functions', href: '#stepfunctions' },
  { type: 'link', text: 'CloudFormation', href: '#cloudformation' },
  { type: 'link', text: 'Redshift', href: '#redshift' },
  { type: 'link', text: 'RDS / Aurora', href: '#rds' },
  { type: 'link', text: 'IAM', href: '#iam' },
]

const PAGES = {
  '#services': ServicesPage,
  '#notebook': NotebookPage,
  '#bedrock': BedrockPage,
  '#sagemaker': SageMakerPage,
  '#s3': S3Page,
  '#dynamodb': DynamoDBPage,
  '#stepfunctions': StepFunctionsPage,
  '#cloudformation': CloudFormationPage,
  '#redshift': RedshiftPage,
  '#rds': RdsPage,
  '#iam': IamPage,
}

export default function App() {
  const [activePage, setActivePage] = useState('#services')
  const PageComponent = PAGES[activePage] || ServicesPage

  return (
    <>
      <div id="top-nav">
        <TopNavigation
          identity={{
            title: 'oblako',
            href: '#services',
            logo: {
              src: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24'%3E%3Cpath d='M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96z' fill='white'/%3E%3C/svg%3E",
              alt: 'oblako',
            },
          }}
          utilities={[
            { type: 'button', text: 'v0.1.0' },
          ]}
        />
      </div>
      <AppLayout
        navigation={
          <SideNavigation
            header={{ text: 'Services', href: '#services' }}
            activeHref={activePage}
            items={NAV_ITEMS}
            onFollow={e => { e.preventDefault(); setActivePage(e.detail.href) }}
          />
        }
        content={<PageComponent />}
        toolsHide={true}
      />
    </>
  )
}
