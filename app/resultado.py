"""Resultado da leitura e motivos de falha (sem dependência do OpenCV).

Fica separado do decoder para que o processo da API (que não decodifica,
só repassa a imagem ao processo leitor) não precise carregar o OpenCV.
"""

from dataclasses import dataclass, field

# Motivos técnicos (enumerados, estáveis) e a orientação em pt-BR para o usuário final.
ORIENTACOES = {
    "sem_qr": (
        "Não encontrei o QR Code na foto. Envie uma nova foto com o QR Code "
        "inteiro aparecendo, de perto, com boa luz e sem reflexo."
    ),
    "qr_ilegivel": (
        "Encontrei o QR Code, mas não consegui lê-lo. Envie uma nova foto mais "
        "de perto, com o cupom esticado, boa luz e sem reflexo."
    ),
    "tempo_esgotado": (
        "Não consegui ler o QR Code a tempo. Envie uma nova foto só do QR Code, "
        "mais de perto, com boa luz e sem reflexo."
    ),
    "imagem_invalida": "Não consegui abrir a imagem. Envie a foto novamente.",
    "tipo_nao_suportado": "Formato de imagem não aceito. Envie a foto em JPEG ou PNG.",
    "muito_grande": "A imagem é grande demais. Envie uma foto comum do celular (até 10 MB).",
    "pixels_demais": "A imagem é grande demais. Envie uma foto comum do celular.",
    "ocupado": "O leitor está ocupado. Tente novamente em alguns segundos.",
    "erro_interno": "Não consegui ler o QR Code agora. Envie a foto novamente em instantes.",
}
MOTIVOS = tuple(ORIENTACOES)


@dataclass
class DecodeResult:
    success: bool
    codes: list[str] = field(default_factory=list)
    enhancements_applied: list[str] = field(default_factory=list)
    attempts: int = 0
    decoder: str | None = None
    estrategia: str | None = None
    motivo: str | None = None
    qr_detectado: bool = False
    analysis: dict = field(default_factory=dict)
    suggestion: str | None = None
    processing_time_ms: float = 0.0
    pre_reduzida: bool = False

    @property
    def orientacao(self) -> str | None:
        return ORIENTACOES.get(self.motivo) if self.motivo else None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "codes": self.codes,
            "motivo": self.motivo,
            "orientacao": self.orientacao,
            "qr_detectado": self.qr_detectado,
            "estrategia": self.estrategia,
            "decoder": self.decoder,
            "attempts": self.attempts,
            "enhancements_applied": self.enhancements_applied,
            "analysis": self.analysis,
            "suggestion": self.suggestion,
            "processing_time_ms": self.processing_time_ms,
        }
