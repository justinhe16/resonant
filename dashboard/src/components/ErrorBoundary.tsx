import { Component, type ReactNode } from "react";
import { TriangleAlert } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

interface State {
  error: Error | null;
}

/** Keeps one bad section from blanking the whole dashboard. */
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
        <Alert variant="bad">
          <TriangleAlert />
          <AlertTitle>This section couldn't be shown</AlertTitle>
          <AlertDescription className="break-words">{this.state.error.message}</AlertDescription>
        </Alert>
      );
    }
    return this.props.children;
  }
}
