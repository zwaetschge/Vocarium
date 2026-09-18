import {useLocation} from 'react-router-dom';
import {AREAS,type Area} from '../lib/navigation';
import {useWorkspaces} from '../state/Workspaces';
const steps:Record<Area,string[]>={
 audiobooks:['Buch importieren','Stimme wählen','Kapitel hören & lesen','Offline sichern oder exportieren'],
 podcasts:['Thema & Quellen','Sprecher & Skript','Audio produzieren','Anhören & exportieren'],
 hoerspiele:['Roman & Serie','Text und Szenen abgleichen','Passagen prüfen','Hörspiel rendern & exportieren'],
 lab:['Text oder Audio','Stimme & Optionen','Erzeugen oder transkribieren','Ergebnis prüfen & speichern'],
};
export default function WorkflowGuide() {
 const {area}=useWorkspaces();
 const {pathname}=useLocation();
 if (pathname!==AREAS.find(a=>a.id===area)?.home) return null;
 return <details className="workflow-guide"><summary>So funktioniert dieser Arbeitsbereich <span>4 Schritte</span></summary>
  <ol>{steps[area].map((step,i)=><li key={step}><span>{i+1}</span>{step}</li>)}</ol>
 </details>;
}
