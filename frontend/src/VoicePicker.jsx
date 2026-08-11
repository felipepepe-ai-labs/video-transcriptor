import { useId } from "react"

/**
 * Piper narration voice, shared by every source that can start a job.
 *
 * The radio group name is generated rather than fixed: two pickers on the same
 * page sharing a name would behave as one group, and selecting a voice in the
 * bookmarks panel would clear the one above it.
 */
function VoicePicker({ value, onChange, label = "Voz de la locución:" }) {
  const name = useId()

  return (
    <div className="voice-picker">
      <span className="voice-picker-label">{label}</span>
      <label className="voice-option">
        <input
          type="radio"
          name={name}
          value="male"
          checked={value === "male"}
          onChange={() => onChange("male")}
        />
        Masculina
      </label>
      <label className="voice-option">
        <input
          type="radio"
          name={name}
          value="female"
          checked={value === "female"}
          onChange={() => onChange("female")}
        />
        Femenina
      </label>
    </div>
  )
}

export default VoicePicker
