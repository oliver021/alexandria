import { render, screen } from '@testing-library/svelte'
import { createRawSnippet } from 'svelte'
import { describe, expect, it } from 'vitest'
import Alert from './Alert.svelte'

const body = createRawSnippet(() => ({ render: () => '<p>Details here</p>' }))

describe('Alert', () => {
  it('announces danger alerts assertively', () => {
    render(Alert, { props: { tone: 'danger', title: 'Broken', children: body } })
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('Broken')
    expect(alert).toHaveTextContent('Details here')
    expect(alert).toHaveClass('alert-danger')
  })

  it('announces the other tones politely', () => {
    render(Alert, { props: { tone: 'success', children: body } })
    expect(screen.getByRole('status')).toHaveClass('alert-success')
  })

  it('can stay silent for static notes', () => {
    const { container } = render(Alert, { props: { tone: 'info', live: false, children: body } })
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(container.querySelector('.alert-info')).toBeInTheDocument()
  })

  it('is not a focus target unless the page asks for it', () => {
    render(Alert, { props: { tone: 'danger', children: body } })
    const alert = screen.getByRole('alert')
    expect(alert).not.toHaveAttribute('id')
    expect(alert).not.toHaveAttribute('tabindex')
  })

  it('can be given the focus by code, but not by Tab, once a page asks for it', () => {
    render(Alert, { props: { tone: 'danger', id: 'failure', focusable: true, children: body } })
    const alert = screen.getByRole('alert')
    expect(alert).toHaveAttribute('id', 'failure')
    expect(alert).toHaveAttribute('tabindex', '-1')
    alert.focus()
    expect(alert).toHaveFocus()
  })

  it('hides its icon from assistive technology', () => {
    const { container } = render(Alert, { props: { tone: 'warning', children: body } })
    expect(container.querySelector('svg')).toHaveAttribute('aria-hidden', 'true')
  })
})
