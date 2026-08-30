"use client";

import { Activity, Archive, Cloud, Download, Plus, Router, ShieldCheck } from "lucide-react";

const devices = [
  { name: "Core Gateway", ip: "10.10.0.1", model: "CCR2004", status: "Защищён", last: "Сегодня, 03:00" },
  { name: "Office North", ip: "10.20.0.1", model: "RB5009", status: "Защищён", last: "Сегодня, 03:04" },
  { name: "Warehouse", ip: "10.30.0.1", model: "hEX S", status: "Ожидает", last: "Вчера, 03:02" },
];

export default function Home() {
  return (
    <main className="min-h-screen bg-[#07110f] text-[#edf8f3]">
      <div className="mx-auto max-w-[1480px] px-5 py-5 lg:px-8">
        <header className="flex items-center justify-between border-b border-white/10 pb-5">
          <div className="flex items-center gap-3"><div className="grid size-10 place-items-center rounded-xl bg-[#b8f34a] text-[#07110f]"><Archive size={20}/></div><div><p className="text-[11px] uppercase tracking-[.2em] text-[#8aa299]">Backup control</p><h1 className="font-semibold tracking-tight">RouterVault</h1></div></div>
          <button className="flex items-center gap-2 rounded-xl bg-[#b8f34a] px-4 py-2.5 text-sm font-semibold text-[#07110f]"><Plus size={17}/> Добавить роутер</button>
        </header>
        <section className="grid gap-8 py-8 lg:grid-cols-[1.55fr_.75fr]">
          <div>
            <div className="mb-8"><p className="mb-2 flex items-center gap-2 text-sm text-[#98aea6]"><Activity size={15} className="text-[#b8f34a]"/> Все системы работают</p><h2 className="max-w-3xl text-4xl font-medium leading-[1.05] tracking-[-.04em] sm:text-6xl">Бэкапы сети<br/><span className="text-[#8da098]">без ручной рутины.</span></h2></div>
            <div className="grid gap-3 sm:grid-cols-3">
              {[{icon:Router,label:"Роутеров",value:"12"},{icon:ShieldCheck,label:"Успешно за 30 дней",value:"99,8%"},{icon:Cloud,label:"Хранилище S3",value:"2,4 ГБ"}].map(({icon:Icon,label,value})=><div key={label} className="rounded-2xl border border-white/10 bg-white/[.045] p-5"><Icon className="mb-7 text-[#b8f34a]" size={20}/><div className="text-3xl font-medium tracking-tight">{value}</div><div className="mt-1 text-xs text-[#81958e]">{label}</div></div>)}
            </div>
          </div>
          <aside className="rounded-3xl bg-[#b8f34a] p-6 text-[#102018] lg:p-8"><p className="text-xs font-semibold uppercase tracking-[.18em] opacity-60">Следующий запуск</p><div className="mt-5 text-5xl font-medium tracking-[-.05em]">03:00</div><p className="mt-2 text-sm opacity-65">через 6 часов 42 минуты</p><div className="my-7 h-px bg-black/15"/><p className="text-sm leading-6 opacity-75">Автоматический бэкап всех активных устройств. Хранение: 30 последних копий.</p><button className="mt-8 w-full rounded-xl bg-[#102018] py-3 text-sm font-semibold text-white">Запустить сейчас</button></aside>
        </section>
        <section className="overflow-hidden rounded-3xl border border-white/10 bg-[#0b1714]">
          <div className="flex items-end justify-between border-b border-white/10 px-5 py-5 sm:px-7"><div><p className="text-xs uppercase tracking-[.16em] text-[#71877f]">Инфраструктура</p><h3 className="mt-1 text-xl font-medium">Устройства и последние копии</h3></div><button className="text-sm text-[#b8f34a]">История бэкапов →</button></div>
          <div className="divide-y divide-white/10">
            {devices.map((d,i)=><div key={d.ip} className="grid items-center gap-4 px-5 py-5 transition hover:bg-white/[.025] sm:grid-cols-[1.3fr_1fr_1fr_auto] sm:px-7"><div className="flex items-center gap-4"><div className="grid size-10 place-items-center rounded-xl bg-white/[.06] text-[#9dafa8]"><Router size={18}/></div><div><div className="font-medium">{d.name}</div><div className="mt-0.5 font-mono text-xs text-[#71877f]">{d.ip}</div></div></div><div><div className="text-xs text-[#71877f]">Модель</div><div className="mt-1 text-sm">{d.model}</div></div><div><div className="text-xs text-[#71877f]">Последний бэкап</div><div className="mt-1 text-sm">{d.last}</div></div><div className="flex items-center justify-between gap-4"><span className={`rounded-full px-3 py-1.5 text-xs ${i===2?"bg-amber-300/10 text-amber-300":"bg-[#b8f34a]/10 text-[#b8f34a]"}`}>{d.status}</span><button aria-label={`Скачать бэкап ${d.name}`} className="grid size-9 place-items-center rounded-lg border border-white/10 text-[#98aea6] hover:text-white"><Download size={16}/></button></div></div>)}
          </div>
        </section>
      </div>
    </main>
  );
}
