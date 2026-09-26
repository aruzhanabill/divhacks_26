import { MapsApp } from "@/components/MapsApp";

export default function Home() {
  return <MapsApp apiKey={process.env.NEXT_PUBLIC_GOOGLE_MAPS_API_KEY ?? ""} />;
}
