export const sampleTextsByLanguage: Record<string, string[]> = {
  English: [
    'The quick brown fox jumps over the lazy dog. A wonderful serenity has taken possession of my entire soul.',
    'In the beginning, the universe was created. This has made a lot of people very angry and been widely regarded as a bad move.',
    'Technology is best when it brings people together. Innovation distinguishes between a leader and a follower.',
    'Hello, welcome to Vocarium. This is a preview of a designed voice.',
  ],
  German: [
    'Es war einmal ein kleiner Junge, der träumte davon, die Sterne zu berühren. Jeden Abend schaute er hinauf zum Himmel.',
    'Die Kunst besteht darin, das Einfache kompliziert zu denken und dann wieder einfach zu sagen.',
    'Technologie ist am besten, wenn sie Menschen zusammenbringt und das Leben leichter macht.',
    'Hallo, willkommen bei Vocarium. Dies ist eine Vorschau einer gestalteten Stimme.',
  ],
  French: [
    "Il était une fois, dans un petit village au bord de la mer, une jeune fille qui rêvait de voyages lointains.",
    'La simplicité est la sophistication suprême. Chaque détail compte dans la quête de la perfection.',
    "La technologie transforme nos vies, mais c'est l'humain qui reste au cœur de toute innovation.",
    'Bonjour, bienvenue chez Vocarium. Ceci est un aperçu d\'une voix conçue sur mesure.',
  ],
  Spanish: [
    'Había una vez, en un pequeño pueblo junto al mar, una niña que soñaba con recorrer el mundo entero.',
    'La simplicidad es la máxima sofisticación. Cada detalle cuenta en la búsqueda de la perfección.',
    'La tecnología transforma nuestras vidas, pero lo importante siempre son las personas detrás de ella.',
    'Hola, bienvenido a Vocarium. Esta es una vista previa de una voz diseñada.',
  ],
  Italian: [
    "C'era una volta, in un piccolo villaggio sul mare, una bambina che sognava di viaggiare per il mondo.",
    'La semplicità è la sofisticazione suprema. Ogni dettaglio conta nella ricerca della perfezione.',
    'La tecnologia trasforma le nostre vite, ma ciò che conta davvero sono le persone dietro di essa.',
    'Ciao, benvenuto su Vocarium. Questa è un\'anteprima di una voce progettata su misura.',
  ],
  Portuguese: [
    'Era uma vez, numa pequena aldeia à beira-mar, uma menina que sonhava em conhecer o mundo inteiro.',
    'A simplicidade é o último grau de sofisticação. Cada detalhe importa na busca pela perfeição.',
    'A tecnologia transforma vidas, mas o que realmente importa são as pessoas por trás dela.',
    'Olá, bem-vindo ao Vocarium. Esta é uma prévia de uma voz desenhada à medida.',
  ],
  Russian: [
    'Однажды поздним вечером старик сидел у окна и смотрел на звёзды, вспоминая свою молодость.',
    'Настоящая красота природы открывается только тому, кто умеет её видеть и ценить.',
    'Технологии меняют мир, но главное — это люди, которые за ними стоят.',
    'Здравствуйте, добро пожаловать в Vocarium. Это предварительный просмотр созданного голоса.',
  ],
  Chinese: [
    '春天来了,花儿开了,鸟儿在枝头歌唱。这是一个美好的季节,充满了希望和生机。',
    '科技的进步让我们的生活变得更加便利,但真正重要的是我们如何使用它。',
    '在这个快节奏的时代,静下心来读一本好书,是一种难得的享受。',
    '您好,欢迎使用 Vocarium。这是为您精心设计的声音预览。',
  ],
  Japanese: [
    '昔々、あるところにおじいさんとおばあさんがいました。ある日、おじいさんは山へ芝刈りに行きました。',
    '春の朝、桜の花びらが風に舞い、街を淡いピンクに染めていきます。',
    '技術の進歩は人々の生活を変え、新しい可能性を切り開いています。',
    'こんにちは、Vocarium へようこそ。これはデザインされた声のプレビューです。',
  ],
  Korean: [
    '옛날 옛적에 깊은 산속에 작은 마을이 하나 있었습니다. 그 마을 사람들은 모두 친절하고 따뜻했습니다.',
    '봄바람이 부는 날, 벚꽃잎이 하늘을 가득 채우며 춤을 추고 있었습니다.',
    '기술의 발전은 우리의 삶을 변화시키고, 새로운 가능성을 열어줍니다.',
    '안녕하세요, Vocarium에 오신 것을 환영합니다. 디자인된 음성의 미리보기입니다.',
  ],
};

function samplesFor(language: string): string[] {
  return sampleTextsByLanguage[language] ?? sampleTextsByLanguage.English;
}

export function getRandomSample(language: string): string {
  const list = samplesFor(language);
  return list[Math.floor(Math.random() * list.length)];
}

export function getDefaultSample(language: string): string {
  return samplesFor(language)[0];
}

export function isKnownSample(text: string): boolean {
  const trimmed = text.trim();
  if (!trimmed) return true;
  for (const list of Object.values(sampleTextsByLanguage)) {
    if (list.includes(trimmed)) return true;
  }
  return false;
}
