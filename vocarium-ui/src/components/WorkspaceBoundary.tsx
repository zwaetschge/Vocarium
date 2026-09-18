import {Component,type ReactNode} from 'react';

/** A failed workflow never removes mode navigation or the current player. */
export default class WorkspaceBoundary extends Component<{children:ReactNode},{failed:boolean}> {
 state={failed:false};
 static getDerivedStateFromError() {return {failed:true};}
 render() {
  if(this.state.failed) return <section role="alert" className="card studio-section"><h1>Diese Ansicht konnte nicht geladen werden</h1><p>Deine gespeicherten Projekte bleiben erhalten. Versuche es erneut oder öffne einen anderen Arbeitsbereich.</p><button className="btn btn-primary" onClick={()=>this.setState({failed:false})}>Erneut versuchen</button></section>;
  return this.props.children;
 }
}
