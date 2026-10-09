import { Component, type ErrorInfo, type ReactNode } from 'react'

/** Contains a render crash to the section that threw (D-057). Without it,
 *  one bad field anywhere unmounted the whole dashboard — the page simply
 *  disappeared. */
export class ErrorBoundary extends Component<
  { label: string; children: ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`[${this.props.label}] render error:`, error, info.componentStack)
  }

  render() {
    if (this.state.error) {
      return (
        <div className="bg-red-900 border border-red-700 rounded p-4 mb-6 text-left" role="alert">
          <div className="font-semibold">The {this.props.label} section failed to display.</div>
          <div className="text-sm text-red-200 mt-1 font-mono break-words">{this.state.error.message}</div>
          <button
            onClick={() => this.setState({ error: null })}
            className="mt-3 border border-red-500 rounded px-3 py-1 text-sm hover:bg-red-800"
          >
            Try again
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
