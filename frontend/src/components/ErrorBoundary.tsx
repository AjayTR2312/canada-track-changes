import React from 'react';

interface Props { children: React.ReactNode; label?: string }
interface State { error: Error | null }

// Without this, one uncaught render error anywhere in a tab unmounts the whole
// React root and the user just sees a blank page with no clue why.
export default class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error(`[ErrorBoundary${this.props.label ? ` · ${this.props.label}` : ''}]`, error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="panel">
        <div className="error-box">
          ⚠️ This tab hit an error{this.props.label ? ` (${this.props.label})` : ''}:{' '}
          <strong>{this.state.error.message}</strong>
        </div>
        <button className="btn-secondary" style={{ marginTop: '0.75rem' }} onClick={() => this.setState({ error: null })}>
          Try again
        </button>
      </div>
    );
  }
}
