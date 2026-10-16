import { Component, type ReactNode } from "react";
import { Alert } from "@/components/ui/alert";

interface State {
  error: Error | null;
}

/** Keeps one bad page from blanking the whole dashboard. */
interface Props {
  children: ReactNode;
  /** A new value clears the error and retries, e.g. the next snapshot. */
  resetKey: string;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  componentDidUpdate(prev: Props) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null });
  }

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <Alert tone="bad">
          <div className="min-w-0">
            <p className="font-medium">This section couldn't be shown</p>
            <p className="break-words text-muted-foreground">{this.state.error.message}</p>
          </div>
        </Alert>
      );
    }
    return this.props.children;
  }
}
